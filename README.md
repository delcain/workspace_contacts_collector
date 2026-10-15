# Workspace Contacts Collector

Extrai endereços dos cabeçalhos de mensagens, sem acessar corpo ou anexos. Lê caixas do Workspace por delegação de domínio, persiste em SQLite, gera CSV para **revisão**, não para marketing automático.

## Configuração
1. Criar projeto no Google Cloud e habilitar **Gmail API** e **Admin SDK API**.
2. Criar uma *service account* dedicada e configurar **Domain-Wide Delegation** com credenciais apropriadas. Registrar seu **Client ID numérico**.
3. Console de administração do Workspace → Segurança → Controle de acesso e dados → Controles de API → Delegação em todo o domínio → Adicionar novo. Informar o Client ID da conta de serviço e as duas scopes (separadas por vírgula):
   - `https://www.googleapis.com/auth/admin.directory.user.readonly`
   - `https://www.googleapis.com/auth/gmail.readonly`
4. Armazenar o JSON como `service-account.json` em local protegido; **nunca enviar por chat, Git ou e-mail**. Considere credenciais sem chave quando sua infraestrutura permitir.
5. Utilizar conta de administrador adequada para `--admin` e informar internamente os responsáveis pela coleta.

## Instalar
```bash
python -m venv .venv
source .venv/bin/activate   # no Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Teste em uma caixa
```bash
python collector.py --key service-account.json --only-user usuario@seudominio.com --max-messages-per-user 100 --exclude-domain seudominio.com
```

## Descobrir usuários
```bash
python collector.py --key service-account.json --admin admin@seudominio.com --list-users
```

## Coleta completa
```bash
python collector.py --key service-account.json --admin admin@seudominio.com --exclude-domain seudominio.com
```

## Coleta por intervalo e exportação
```bash
python collector.py --key service-account.json --admin admin@seudominio.com --after 2025/01/01 --exclude-domain seudominio.com
python collector.py --export-only --exclude-domain seudominio.com
```

A coleta é retomável: mensagens já marcadas como processadas não requerem nova consulta detalhada, mas a paginação do histórico é repetida. A próxima execução pode acrescentar mensagens novas sem duplicá-las. A busca ignora Spam e Lixeira. Contas suspensas, arquivadas e contas sem Gmail são ignoradas quando identificadas pelo diretório. As mensagens existentes continuam sujeitas às políticas de retenção da organização.

## Logs no console

Todas as etapas geram logs com horário e nível: inicialização, clientes Google, tentativas e duração das requisições, páginas, mensagens já processadas, gravações no SQLite, filtros e linhas do CSV, falhas e encerramento. As respostas da API e os demais dados estruturados são exibidos como JSON com indentação de dois espaços e acentos preservados. Falhas por caixa incluem o traceback.

Os logs são enviados ao stderr, mantendo a lista de `--list-users` no stdout. Eles incluem endereços e cabeçalhos retornados pela API; o conteúdo do arquivo de credenciais e tokens de autenticação não são registrados.

O CSV inclui `status_consentimento=nao_verificado` em todas as linhas. Antes de usar no Listmonk, separar endereços pessoais/colaboradores, contatos que não devem receber comunicações, opt-outs e casos sem base legal. **Não importar a lista indiscriminadamente para marketing.**

Não coloque as credenciais ou o SQLite em diretório público. Mantenha acesso restrito e descarte os dados segundo a política de retenção aprovada.
