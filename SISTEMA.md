# Sistema Sahara

A Sahara atende somente por delivery, em Maringá, de segunda a domingo, das 18h às 23h, no fuso `America/Sao_Paulo`. O cliente pode usar o cardápio sem criar conta. A loja confirma disponibilidade, endereço, prazo e pagamento.

O [painel do PDV](https://sahara-esfihas-pdv.onrender.com/admin.html) e a API estão hospedados no Render. O [cardápio e aplicativo para clientes](https://saharaesfihas-card.github.io/saharaesfihas/) usam essa API para registrar as solicitações.

## O que está implementado

| Recurso | Comportamento atual |
| --- | --- |
| Cardápio e sacola | Produtos, preços, registro da solicitação no PDV e mensagem preparada para envio pelo cliente ao WhatsApp. |
| Endereço e localização | Endereço manual ou ponto de localização conferido e confirmado pelo cliente; o número da casa continua obrigatório. |
| Ajuda no cardápio | Respostas locais sobre produtos, horário, entrega e como pedir. Não é um chatbot com IA nem atende conversas no WhatsApp. |
| Pedidos, PDV e cozinha | Registro persistente de pedidos, histórico e etapas operadas pela loja. O PDV também registra pedidos recebidos por telefone ou WhatsApp. |
| Estoque | Itens, entradas, saídas e indicação de reposição no painel. A API também permite cadastrar fichas técnicas; a entrada do pedido em preparo consome o estoque configurado. Produtos sem vínculo de estoque não têm disponibilidade controlada. |
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

O GitHub Pages publica HTML, CSS, JavaScript e o aplicativo instalável. As solicitações são enviadas para `https://sahara-esfihas-pdv.onrender.com/api`; depois do registro, o cliente revisa e envia a mensagem no WhatsApp. O checkout não oferece agendamento.

O servidor Render executa Python e Node.js, mantém os pedidos em SQLite privado e serve o cardápio, o painel e a API por HTTPS. A configuração entregue pelo servidor em `system-config.js` usa `/api` para o seu próprio cardápio.

O painel administrativo deve ser aberto em `https://sahara-esfihas-pdv.onrender.com/admin.html`, usando a senha configurada no Render. O painel usa a API e o cookie seguro da própria origem.

O arquivo `system-config.js` do GitHub Pages contém somente o endereço público da API. A origem do cardápio está autorizada em `SAHARA_ALLOWED_ORIGINS`; credenciais permanecem no servidor. Antes de mudar esse destino, confira HTTPS, saúde, catálogo e autorização da origem. Os detalhes de implantação e manutenção estão em [HOSPEDAGEM.md](HOSPEDAGEM.md).

## Executar e configurar

Requisitos: Python 3.12, Node.js e dados de fuso horário para `America/Sao_Paulo`. A imagem Docker inclui esses requisitos. O Node lê o catálogo real de `app.js` ao iniciar o servidor; os preços enviados pelo navegador não substituem os preços do catálogo.

Na raiz do projeto:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
.venv/bin/python -m server.set_password
```

O último comando pede e confirma a senha sem exibi-la, e produz seu hash `scrypt`. Configure esse resultado como segredo `SAHARA_ADMIN_PASSWORD_HASH` no ambiente de execução. Não publique a senha ou o hash, não os coloque no repositório e não os envie pelo chat. Sem o hash, o comando Uvicorn usado no exemplo local deixa o login administrativo bloqueado.

Na implantação Docker/Render, `python -m server.run` também aceita `SAHARA_ADMIN_PASSWORD` com pelo menos oito caracteres, informada no campo seguro do provedor. O inicializador gera o hash em memória e retira a senha original do ambiente do processo. Use essa opção **ou** `SAHARA_ADMIN_PASSWORD_HASH`. A execução de produção exige uma credencial válida antes de iniciar.

| Variável | Uso |
| --- | --- |
| `SAHARA_ADMIN_PASSWORD` | Senha exclusiva com pelo menos oito caracteres, aceita pelo inicializador `server.run`; é a opção solicitada pelo Blueprint Render. |
| `SAHARA_ADMIN_PASSWORD_HASH` | Alternativa à senha original: hash gerado pelo comando acima. É a opção usada no exemplo local com Uvicorn direto. |
| `SAHARA_DATA_DIR` | Diretório privado e persistente do banco, fora da pasta do projeto. Por padrão, usa `.sahara-system-data` ao lado do repositório. |
| `SAHARA_COOKIE_SECURE` | Padrão `1`, para HTTPS. Use `0` somente em desenvolvimento local por HTTP. |
| `SAHARA_ALLOWED_ORIGINS` | Origens autorizadas do cardápio, separadas por vírgula. Padrão: `https://saharaesfihas-card.github.io`. Não inclua caminhos de páginas. |
| `PORT` | Porta do inicializador de produção; padrão `10000`. |
| `FORWARDED_ALLOW_IPS` | Proxies autorizados a informar protocolo e IP do cliente. Confie apenas no proxy que protege o serviço. |

Para desenvolvimento local, após configurar o hash no ambiente:

```bash
export SAHARA_COOKIE_SECURE=0
.venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port 8010
```

Abra `http://127.0.0.1:8010/` para o cardápio e `http://127.0.0.1:8010/admin.html` para a gestão. `/api/health` indica se o servidor responde e se a senha administrativa foi configurada. Esse endereço local não publica o sistema na internet.

Em produção, mantenha `SAHARA_COOKIE_SECURE=1`, configure HTTPS e execute o servidor por um gerenciador de processos ou pelo container entregue. Se houver proxy reverso, preserve o `Host` público, encaminhe o protocolo HTTPS corretamente e confie em cabeçalhos de proxy somente do proxy autorizado. O painel deve permanecer no mesmo domínio da API; as alterações exigem sessão e token de proteção contra requisições indevidas. Um protocolo HTTP informado incorretamente pelo proxy pode causar erro de origem ao entrar no painel.

## Layout do painel

O painel tem navegação lateral por operação, gestão, relacionamento e configurações. No celular, o botão de menu abre as mesmas áreas em uma gaveta. Os cartões de pedidos mostram um identificador curto para facilitar a leitura; as alterações e comandas continuam usando o identificador completo.

Cada pedido exibe quatro etapas numeradas: preparo na cozinha, pedido pronto, a caminho do endereço e entregue. A tela de pedidos traz os totais reais por etapa, busca por cliente, telefone ou pedido e filtro de situação. O PDV permite buscar produtos, filtrar categorias e ajustar quantidades sem perder os dados preenchidos. O resumo acompanha os produtos selecionados e seu subtotal; cupons são conferidos pelo servidor ao registrar o pedido. Dados do cliente, endereço e pagamento ficam em seções que podem ser recolhidas.

As mudanças do painel precisam de uma nova implantação no serviço Render existente. Com a implantação automática desligada, use **Manual Deploy → Deploy latest commit**; o GitHub Pages publica o cardápio separadamente.

## Uso diário

1. Entre em `admin.html` e abra o caixa.
2. Cadastre o estoque que deseja controlar e registre entradas antes de receber pedidos. Insumos sem ficha técnica não são consumidos automaticamente.
3. Novos pedidos do cardápio e do PDV entram em **Em preparo na cozinha**. Confira o pedido e avance para **Pedido pronto**, **A caminho do endereço** e **Entregue**, conforme cada etapa ocorrer. O sistema exige o pedido pronto antes da saída para entrega.
4. Registre o pagamento somente depois de receber o valor. Esse registro entra no caixa e no relatório; não aciona banco ou operadora de cartão.
5. Registre fiado e seus recebimentos na tela correspondente. Cada recebimento entra no caixa. O painel bloqueia o cancelamento de pedidos vinculados que já tenham recebimentos.
6. Consulte clientes e fidelidade; crie campanhas apenas como rascunhos até haver integração oficial de envio.
7. Confira as movimentações e feche o caixa com o valor efetivamente contado.

Novos pedidos consomem o estoque vinculado e registram o uso do cupom na mesma transação de entrada em preparo. Uma repetição do registro ou da etapa não duplica esses movimentos. Se faltar estoque, o pedido não é registrado. O cancelamento depois do início do preparo não devolve automaticamente os insumos. Pedidos antigos ainda em confirmação preservam a devolução de estoque ao cancelar antes do preparo. O painel bloqueia o cancelamento de pedidos pagos ou com recebimento parcial: a rotina de cancelamento com estorno ainda não está disponível. Um ajuste manual no caixa não desbloqueia esse cancelamento e não realiza estorno em provedores externos.

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
