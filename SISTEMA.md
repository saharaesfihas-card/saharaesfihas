# Sistema Sahara

A Sahara atende somente por delivery, em Maringá, de segunda a domingo, das 18h às 23h, no fuso `America/Sao_Paulo`. O cliente pode usar o cardápio sem criar conta. A loja confirma disponibilidade, endereço, prazo e pagamento.

## O que está implementado

| Recurso | Comportamento atual |
| --- | --- |
| Cardápio e sacola | Produtos, preços e pedido preparado para envio pelo cliente ao WhatsApp. |
| Endereço e localização | Endereço manual ou ponto de localização conferido e confirmado pelo cliente; o número da casa continua obrigatório. |
| Solicitação de agendamento | Data e horário futuros, até sete dias à frente, entre 18h e 23h. Depende da confirmação da loja. |
| Ajuda no cardápio | Respostas locais sobre produtos, horário, entrega e como pedir. Não é um chatbot com IA nem atende conversas no WhatsApp. |
| Pedidos, PDV e cozinha | Com o servidor ativo, registro persistente de pedidos, histórico e etapas operadas pela loja. O PDV registra pedidos recebidos por telefone ou WhatsApp. |
| Estoque | Itens, entradas, saídas e indicação de reposição no painel. A API também permite cadastrar fichas técnicas; a confirmação do pedido consome o estoque configurado. Produtos sem vínculo de estoque não têm disponibilidade controlada. |
| Caixa e fiado | Abertura, fechamento, movimentações, registro manual de pagamento e recebimentos de fiado. Registrar um recebimento não realiza uma cobrança online. |
| Clientes e RFV | Cadastro, histórico e segmentação por recência, frequência e valor de pedidos entregues e pagos. |
| Cupons | Cadastro de descontos com validade e limites; aplicação no PDV e na API de pedidos. O checkout público ainda não tem um campo de cupom. |
| Fidelidade | Um ponto por R$ 1 do total líquido de cada pedido entregue e pago, arredondado para baixo; benefícios e resgates registrados no painel. |
| Campanhas | Rascunhos com público que autorizou ofertas. Nenhuma mensagem é disparada. |
| Entregadores | Cadastro, atribuição manual a pedidos e abertura do endereço no mapa. Sem localização ao vivo ou cálculo automático de rotas. |
| Comandas | Impressão pelo navegador. A API guarda perfis de largura e quantidade de cópias; não conecta impressoras de rede nem imprime automaticamente. |
| Avaliações | Recebimento pela API com o token do pedido entregue e consulta no painel. |
| Resultados | Pedidos, vendas com pagamento registrado, ticket médio e vendas por dia. A conversão depende dos eventos de visita efetivamente registrados. |

Os módulos de gestão usam um banco SQLite privado no servidor. Eles não dependem de dados demonstrativos nem do armazenamento do navegador para manter os registros.

## Integrações que precisam de ativação

O painel informa estas integrações como **pendentes**, com o motivo de cada pendência:

- WhatsApp Business Platform, chatbot com IA e envio de campanhas;
- pagamento online com cartão ou Pix e confirmação por webhook;
- emissão fiscal;
- iFood, Entrega Fácil e F360;
- Meta Ads, Google Ads, Analytics e GTM;
- recuperação automática de carrinho;
- impressão automática em múltiplas impressoras.

Esses serviços exigem contas, autorização e configuração dos respectivos provedores. Não há integração ativa com a Cardápio Web. A seleção de Pix ou cartão no cardápio apenas informa a preferência de pagamento à loja. Mesas e comandas de salão não fazem parte desta operação de delivery.

## Site estático e servidor de gestão

O GitHub Pages publica HTML, CSS, JavaScript e o aplicativo instalável. Ele **não executa o servidor Python**. Nessa hospedagem, o pedido continua pelo WhatsApp; o painel mostra a pendência de ativação quando não encontra a API.

Para receber os pedidos no painel, hospede o servidor em um serviço com Python, Node.js, disco persistente privado e HTTPS. O mesmo servidor serve o cardápio, o painel e a API. A configuração entregue por ele em `system-config.js` ativa automaticamente `/api` para o cardápio.

Também é possível manter o cardápio no GitHub Pages e definir em `system-config.js` um `apiBase` com o endereço HTTPS público da API. Esse arquivo pode conter apenas o endereço do serviço, nunca credenciais. Nesse caso, autorize a origem do cardápio em `SAHARA_ALLOWED_ORIGINS` e abra o painel no domínio do servidor: o painel administrativo usa a API da própria origem.

## Executar e configurar

Requisitos: Python 3.12 e Node.js. O Node lê o catálogo real de `app.js` ao iniciar o servidor; os preços enviados pelo navegador não substituem os preços do catálogo.

Na raiz do projeto:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
.venv/bin/python -m server.set_password
```

O último comando pede e confirma a senha sem exibi-la, e produz seu hash `scrypt`. Configure esse resultado como segredo `SAHARA_ADMIN_PASSWORD_HASH` no ambiente de execução. Não publique a senha ou o hash, não os coloque no repositório e não os envie pelo chat. Sem essa variável, o login administrativo fica bloqueado.

| Variável | Uso |
| --- | --- |
| `SAHARA_ADMIN_PASSWORD_HASH` | Hash gerado pelo comando acima; necessário para entrar no painel. |
| `SAHARA_DATA_DIR` | Diretório privado e persistente do banco, fora da pasta do projeto. Por padrão, usa `.sahara-system-data` ao lado do repositório. |
| `SAHARA_COOKIE_SECURE` | Padrão `1`, para HTTPS. Use `0` somente em desenvolvimento local por HTTP. |
| `SAHARA_ALLOWED_ORIGINS` | Origens autorizadas do cardápio, separadas por vírgula. Padrão: `https://saharaesfihas-card.github.io`. Não inclua caminhos de páginas. |

Para desenvolvimento local, após configurar o hash no ambiente:

```bash
export SAHARA_COOKIE_SECURE=0
.venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8010
```

Abra `http://127.0.0.1:8010/` para o cardápio e `http://127.0.0.1:8010/admin.html` para a gestão. `/api/health` indica se o servidor responde e se a senha administrativa foi configurada. Esse endereço local não publica o sistema na internet.

Em produção, mantenha `SAHARA_COOKIE_SECURE=1`, configure HTTPS e execute o servidor por um gerenciador de processos. Se houver proxy reverso, encaminhe a origem HTTPS corretamente e confie em cabeçalhos de proxy somente do proxy autorizado. O painel deve permanecer no mesmo domínio da API; as alterações exigem sessão e token de proteção contra requisições indevidas.

## Uso diário

1. Entre em `admin.html` e abra o caixa.
2. Cadastre o estoque que deseja controlar e registre entradas antes de confirmar pedidos. Insumos sem ficha técnica não são consumidos automaticamente.
3. Confira o pedido e o eventual agendamento antes de confirmá-lo. Atualize as etapas conforme preparo, saída e entrega ocorrerem.
4. Registre o pagamento somente depois de receber o valor. Esse registro entra no caixa e no relatório; não aciona banco ou operadora de cartão.
5. Registre fiado e seus recebimentos na tela correspondente. Cada recebimento entra no caixa. O painel bloqueia o cancelamento de pedidos vinculados que já tenham recebimentos.
6. Consulte clientes e fidelidade; crie campanhas apenas como rascunhos até haver integração oficial de envio.
7. Confira as movimentações e feche o caixa com o valor efetivamente contado.

Cancelar um pedido confirmado, antes do preparo, devolve o estoque consumido. Depois do início do preparo, o estoque não retorna automaticamente. O painel bloqueia o cancelamento de pedidos pagos ou com recebimento parcial: a rotina de cancelamento com estorno ainda não está disponível. Um ajuste manual no caixa não desbloqueia esse cancelamento e não realiza estorno em provedores externos.

## Dados e manutenção

Mantenha o banco fora de qualquer diretório publicado. O servidor rejeita `SAHARA_DATA_DIR` dentro do repositório, e não entrega arquivos do servidor ou do banco como conteúdo público. Use permissões restritas no diretório e no serviço que o executa.

Faça cópias de segurança frequentes pelo mecanismo de backup do SQLite, ou pare o servidor antes de copiar o diretório de dados inteiro. Com o servidor ativo, copiar apenas `sahara.sqlite3` pode deixar de fora alterações presentes no arquivo WAL. Guarde as cópias em armazenamento privado, teste a restauração e defina retenção adequada para os dados de clientes. O disco do servidor precisa persistir entre reinícios e novas publicações.

Mudanças no catálogo são carregadas no início do servidor. Após atualizar os produtos e publicar o código, reinicie o serviço; pedidos existentes preservam os preços e produtos registrados no momento da criação.

As informações preparadas para cadastro no Google estão em [GOOGLE.md](GOOGLE.md). O cadastro da loja no Google e a verificação da conta são separados da publicação deste sistema.

## Verificação

Na raiz do projeto, instale também as dependências de desenvolvimento e execute os testes da API:

```bash
.venv/bin/python -m pip install -r server/requirements-dev.txt
.venv/bin/python -m unittest discover -s server/tests -v
```

Os testes de navegador exigem o módulo Node `playwright` e o Chromium em `/usr/bin/chromium`, disponíveis no ambiente de nuvem configurado:

```bash
PYTHON=.venv/bin/python node tests/admin.spec.cjs
node tests/address.spec.cjs
node tests/ordering.spec.cjs
```

Esses testes iniciam e encerram seus próprios servidores; os testes de gestão usam banco temporário e não enviam mensagens ou cobranças a serviços externos.

O teste de atualização e funcionamento offline do aplicativo usa o servidor normal em `127.0.0.1:8010`. Mantenha o comando Uvicorn da seção de execução ativo em outro terminal e execute:

```bash
node tests/pwa.spec.cjs
```
