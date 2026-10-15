#!/usr/bin/env python3
"""Coletor corporativo de endereços do Google Workspace (somente metadados)."""
import argparse
import csv
import datetime as dt
import email.utils
import json
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

DIR_SCOPE = 'https://www.googleapis.com/auth/admin.directory.user.readonly'
GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'
HEADERS = ['From', 'To', 'Cc', 'Bcc', 'Date']
LOG = logging.getLogger('collector')


def log_data(stage, data):
    """Exibe dados estruturados com indentação e caracteres legíveis."""
    LOG.info('%s:\n%s', stage, json.dumps(data, indent=2, ensure_ascii=False, default=str))


SCHEMA = '''
CREATE TABLE IF NOT EXISTS processed (
 mailbox TEXT NOT NULL, message_id TEXT NOT NULL, processed_at TEXT NOT NULL,
 PRIMARY KEY (mailbox, message_id)
);
CREATE TABLE IF NOT EXISTS interactions (
 mailbox TEXT NOT NULL, message_id TEXT NOT NULL, email TEXT NOT NULL,
 direction TEXT NOT NULL, display_name TEXT, when_utc TEXT,
 PRIMARY KEY (mailbox, message_id, email, direction)
);
CREATE INDEX IF NOT EXISTS idx_interactions_email ON interactions(email);
'''


def utc_iso(timestamp_ms):
    try:
        return dt.datetime.fromtimestamp(int(timestamp_ms)/1000, tz=dt.timezone.utc).isoformat()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def normalize(address):
    address = address.strip().lower()
    if not address or len(address) > 254 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', address):
        return None
    return address


def parse_addresses(header_values):
    for name, addr in email.utils.getaddresses(header_values):
        normalized = normalize(addr)
        if normalized:
            yield normalized, ' '.join(name.strip().split())[:255]


def credentials(keyfile, subject, scopes):
    credentials = service_account.Credentials.from_service_account_file(str(keyfile), scopes=scopes)
    return credentials.with_subject(subject)


def service(name, version, keyfile, subject, scopes):
    LOG.info('Preparando cliente %s %s para %s', name, version, subject)
    client = build(name, version, credentials=credentials(keyfile, subject, scopes), cache_discovery=False)
    LOG.info('Cliente %s pronto para %s', name, subject)
    return client


def call(request, retries=7):
    operation = str(getattr(request, 'methodId', 'Google API'))
    for retry in range(retries):
        try:
            LOG.info('Requisição %s; tentativa %d/%d', operation, retry + 1, retries)
            started = time.monotonic()
            result = request.execute(num_retries=0)
            LOG.info('Requisição %s concluída em %.2fs', operation, time.monotonic() - started)
            log_data('Resposta ' + operation, result)
            return result
        except HttpError as exc:
            status = exc.resp.status
            LOG.warning('Requisição %s retornou HTTP %s', operation, status)
            try:
                log_data('Erro da API', json.loads(exc.content.decode('utf-8', errors='replace')))
            except (ValueError, AttributeError):
                LOG.warning('Resposta de erro não contém JSON válido')
            retryable = status in (429, 500, 502, 503, 504)
            if status == 403:
                try:
                    info = json.loads(exc.content.decode('utf-8', errors='replace'))
                    reason = (info.get('error', {}).get('errors') or [{}])[0].get('reason', '')
                    retryable = reason in ('rateLimitExceeded', 'userRateLimitExceeded', 'backendError')
                except (ValueError, IndexError, AttributeError):
                    pass
            if not retryable or retry == retries-1:
                raise
            delay = min(2**retry, 64)
            LOG.warning('Google API %s; nova tentativa em %ss', status, delay)
            time.sleep(delay)


def list_users(admin):
    token = None
    page = 0
    while True:
        page += 1
        LOG.info('Listando usuários: página %d', page)
        result = call(admin.users().list(customer='my_customer', maxResults=500,
                          orderBy='email', projection='basic', pageToken=token,
                          fields='nextPageToken,users(primaryEmail,suspended,archived,isMailboxSetup)'))
        for user in result.get('users', []):
            if not user.get('suspended', False) and not user.get('archived', False) and user.get('isMailboxSetup', True):
                yield user['primaryEmail'].lower()
            else:
                LOG.info('Usuário ignorado (suspenso, arquivado ou sem Gmail): %s', user.get('primaryEmail'))
        token = result.get('nextPageToken')
        if not token:
            break


def scan_mailbox(conn, keyfile, mailbox, query=None, limit=0):
    log_data('Iniciando coleta da caixa', {'mailbox': mailbox, 'query': query, 'limit': limit})
    gmail = service('gmail', 'v1', keyfile, mailbox, [GMAIL_SCOPE])
    seen = added = 0
    token = None
    page = 0
    while True:
        page += 1
        LOG.info('%s: listando página %d', mailbox, page)
        request = gmail.users().messages().list(userId='me', maxResults=500,
                     pageToken=token, q=query, includeSpamTrash=False,
                     fields='nextPageToken,messages(id)')
        result = call(request)
        for item in result.get('messages', []):
            if limit and seen >= limit:
                break
            seen += 1
            message_id = item['id']
            LOG.info('%s: mensagem %d; id=%s; verificando SQLite', mailbox, seen, message_id)
            if conn.execute('SELECT 1 FROM processed WHERE mailbox=? AND message_id=?',
                            (mailbox, message_id)).fetchone():
                LOG.info('%s: mensagem %s já processada; ignorando', mailbox, message_id)
                continue
            try:
                data = call(gmail.users().messages().get(
                    userId='me', id=message_id, format='metadata', metadataHeaders=HEADERS,
                    fields='id,internalDate,payload(headers),labelIds'))
            except HttpError as exc:
                if exc.resp.status == 404:
                    LOG.warning('%s: mensagem %s excluída durante a coleta; ignorando', mailbox, message_id)
                    continue  # mensagem excluída durante o processamento
                raise
            headers = defaultdict(list)
            for h in data.get('payload', {}).get('headers', []):
                headers[h.get('name', '').lower()].append(h.get('value', ''))
            is_sent = 'SENT' in data.get('labelIds', [])
            # SENT => From é interno/remetente; To/Cc/Bcc são destinatários.
            # Recebida => From é remetente externo; To/Cc/Bcc são participantes.
            direction_map = {'from': 'sent_from' if is_sent else 'received_from',
                             'to': 'sent_to' if is_sent else 'received_to',
                             'cc': 'sent_cc' if is_sent else 'received_cc',
                             'bcc': 'sent_bcc' if is_sent else 'received_bcc'}
            when = utc_iso(data.get('internalDate'))
            interactions = []
            with conn:
                for field, direction in direction_map.items():
                    for addr, display_name in parse_addresses(headers[field]):
                        interactions.append({'email': addr, 'direction': direction, 'display_name': display_name})
                        conn.execute('''INSERT OR IGNORE INTO interactions
                            (mailbox,message_id,email,direction,display_name,when_utc)
                            VALUES (?,?,?,?,?,?)''',
                            (mailbox, message_id, addr, direction, display_name, when))
                conn.execute('INSERT OR IGNORE INTO processed VALUES (?,?,?)',
                             (mailbox, message_id, dt.datetime.now(dt.timezone.utc).isoformat()))
            added += 1
            log_data('Mensagem persistida', {'mailbox': mailbox, 'message_id': message_id,
                     'when_utc': when, 'interactions': interactions})
            if seen % 500 == 0:
                LOG.info('%s: %d mensagens vistas, %d novas processadas', mailbox, seen, added)
        if limit and seen >= limit:
            LOG.info('%s: limite de %d mensagens atingido', mailbox, limit)
            break
        token = result.get('nextPageToken')
        if not token:
            break
    LOG.info('%s: concluído; vistas=%d, novas=%d', mailbox, seen, added)
    return seen, added


def export_csv(conn, dest, excluded_domains):
    """Um endereço único por linha. Não presume autorização para marketing."""
    log_data('Iniciando exportação CSV', {'destino': dest, 'dominios_excluidos': sorted(excluded_domains)})
    rows = conn.execute('''SELECT email,
            MAX(CASE WHEN display_name != '' THEN display_name ELSE NULL END) AS name,
            COUNT(DISTINCT mailbox || ':' || message_id) AS message_count,
            MIN(when_utc) AS first_seen, MAX(when_utc) AS last_seen,
            COUNT(DISTINCT mailbox) AS mailboxes,
            SUM(CASE WHEN direction LIKE 'sent_%' THEN 1 ELSE 0 END) AS occurrences_sent,
            SUM(CASE WHEN direction LIKE 'received_%' THEN 1 ELSE 0 END) AS occurrences_received
            FROM interactions GROUP BY email ORDER BY message_count DESC, email''')
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dest.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['email','nome','mensagens','primeira_interacao_utc','ultima_interacao_utc',
                         'caixas_com_interacao','ocorrencias_enviadas','ocorrencias_recebidas','status_consentimento'])
        for email_address, name, count, first, last, mailboxes, sent, received in rows:
            domain = email_address.rsplit('@', 1)[-1]
            if domain in excluded_domains:
                LOG.info('CSV: endereço excluído por domínio: %s', email_address)
                continue
            if email_address.split('@',1)[0].startswith(('no-reply','noreply','donotreply','mailer-daemon')):
                LOG.info('CSV: endereço automático ignorado: %s', email_address)
                continue
            writer.writerow([email_address, name or '', count, first or '', last or '',
                             mailboxes, sent or 0, received or 0, 'nao_verificado'])
            written += 1
            LOG.info('CSV: linha %d gravada; endereço=%s', written, email_address)
    LOG.info('Exportados %d endereços para %s', written, dest)


def main():
    ap = argparse.ArgumentParser(description='Extrator read-only de contatos históricos do Google Workspace')
    ap.add_argument('--key', type=Path, default=Path('service-account.json'), help='JSON da conta de serviço')
    ap.add_argument('--admin', help='E-mail do administrador autorizado a consultar usuários')
    ap.add_argument('--db', type=Path, default=Path('contacts.sqlite3'))
    ap.add_argument('--out', type=Path, default=Path('contacts_review.csv'))
    ap.add_argument('--only-user', help='Testar apenas uma caixa (ignora Directory API)')
    ap.add_argument('--list-users', action='store_true', help='Somente listar usuários elegíveis')
    ap.add_argument('--export-only', action='store_true', help='Exportar sem consultar Google')
    ap.add_argument('--after', help='Data inicial YYYY/MM/DD usando busca do Gmail')
    ap.add_argument('--limit-users', type=int, default=0)
    ap.add_argument('--max-messages-per-user', type=int, default=0)
    ap.add_argument('--exclude-domain', action='append', default=[], help='Domínio interno a excluir do CSV; repetível')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    started = time.monotonic()
    log_data('Iniciando execução; parâmetros', vars(args))
    if args.after:
        try:
            dt.datetime.strptime(args.after, '%Y/%m/%d')
        except ValueError:
            ap.error('--after precisa ser YYYY/MM/DD')
    if not args.export_only and not args.only_user and not args.admin:
        ap.error('--admin é obrigatório para listar usuários')
    if not args.export_only and not args.key.is_file():
        ap.error('Arquivo de credenciais não encontrado: ' + str(args.key))
    if args.limit_users < 0 or args.max_messages_per_user < 0:
        ap.error('Limites precisam ser maiores ou iguais a zero')
    LOG.info('Abrindo SQLite: %s', args.db)
    conn = sqlite3.connect(str(args.db))
    conn.executescript(SCHEMA)
    LOG.info('SQLite pronto; tabelas e índices verificados')
    try:
        if not args.export_only:
            if args.only_user:
                users = [args.only_user.lower()]
            else:
                admin = service('admin', 'directory_v1', args.key, args.admin, [DIR_SCOPE])
                users = list(list_users(admin))
            if args.limit_users:
                users = users[:args.limit_users]
            LOG.info('Usuários selecionados: %d', len(users))
            log_data('Caixas selecionadas', users)
            if args.list_users:
                for user in users:
                    print(user)
                return
            failures = []
            for idx, user in enumerate(users, 1):
                LOG.info('Caixa %d/%d: %s', idx, len(users), user)
                try:
                    scan_mailbox(conn, args.key, user,
                                 query=f'after:{args.after}' if args.after else None,
                                 limit=args.max_messages_per_user)
                except (HttpError, Exception) as exc:
                    # Falhas isoladas não interrompem o restante, mas geram status não zero.
                    LOG.exception('Falha na caixa %s: %s', user, exc)
                    failures.append(user)
            export_csv(conn, args.out, {d.lower().lstrip('@') for d in args.exclude_domain})
            if failures:
                LOG.error('Caixas com falha (%d): %s', len(failures), ', '.join(failures))
                sys.exit(2)
        else:
            export_csv(conn, args.out, {d.lower().lstrip('@') for d in args.exclude_domain})
    finally:
        conn.close()
        LOG.info('Conexão SQLite encerrada; execução finalizada em %.2fs', time.monotonic() - started)


if __name__ == '__main__':
    main()
