# iFood: loja de teste e preparação das lojas reais

## Três lojas reais

Em **Integrações → iFood · três lojas reais**, o PDV mantém cadastros privados
separados para **Esfihas 44**, **Sahara Esfihas e Shawarma** e **Maktub Esfihas**.
O proprietário confirmou que os produtos e preços diferem entre elas.

Cada cadastro aceita o Merchant UUID da respectiva loja real, que pode ficar em
branco até ser identificado. Salvar esse identificador mantém a situação
**Produção pendente**. O proprietário cadastrou **Sahara PDV**, centralizado,
na nova conta organizacional; a última tela confirmada mostra **Em desenvolvimento**.
A nova conta também contém **Teste (C)**, centralizado, e **Teste (D)**, distribuído.
As capturas do **Teste (C)** confirmam Order, Catalog, Events e Merchant
habilitados, uma loja de teste com permissão **Ativa** e webhook **Desativado**.
O Merchant UUID dessa nova loja ainda não foi identificado. A conexão real exige
aplicativo de produção homologado,
autorização de cada loja e implementação/validação do fluxo de pedidos reais.
O cadastro preparatório não publica cardápios nem consulta o iFood.

O **ID numérico no Portal do Parceiro** é guardado em um campo separado, como
referência para identificar cada estabelecimento. Esse número, exibido em
**Escolher loja**, não preenche o campo Merchant UUID nem autoriza a integração.
Ambos os campos são opcionais e podem ser atualizados individualmente pela API;
um campo omitido em uma atualização conserva o valor anterior.

Os identificadores são únicos entre os três cadastros. IDs de teste conhecidos
ou configurados como teste no servidor são rejeitados. Reiniciar ou publicar o
serviço preserva os valores já salvos. O cadastro usa login administrativo,
CSRF e validação de origem, e permanece fora do catálogo público.

O proprietário escolheu **Esfihas 44**, ID do Portal **4172927**, para receber
os **57 produtos e preços normais do site Sahara**. Sahara Esfihas e Shawarma
(Portal **692154**) e Maktub Esfihas (Portal **1263888**) têm cardápios próprios;
seus produtos, preços e fotos ainda precisam ser organizados. Não há cópia
automática de itens, preços ou promoções
entre as lojas. A área de importação de teste abaixo conserva a configuração
existente; cadastrar uma loja real não ativa a importação de seus pedidos.

Para concluir o trabalho: identificar os três Merchant UUIDs e catálogos; obter
as permissões de produção para Order/Events e catálogo; implementar o roteamento
por loja, sincronização de etapas e publicação de catálogo conforme os contratos
oficiais; então validar a operação de cada loja antes da ativação.

## Homologação solicitada

O checklist recebido do suporte define três cenários Merchant (informações da
loja, interrupções e horários) e três Catalog (categoria/item, grupo/dois
complementos e alterações). Cada cenário deve ter vídeo próprio, data/hora e
client_id do aplicativo de teste utilizado, com as conferências no Portal do
Parceiro exigidas pelo roteiro. Os vídeos são entregues por links compartilháveis.
Ainda não foram gravados ou enviados vídeos de homologação.

O texto específico de Order orienta o wizard automático no Developer Portal.
Os cenários reais desse wizard ainda precisam ser conferidos. A mensagem geral
menciona Events, mas o material recebido não traz um roteiro independente desse
módulo; sua participação deve ser conferida no portal/chamado.

O proprietário forneceu as quatro coleções oficiais do Postman obtidas em
**Documentações → Collections → Restaurante**: Merchant v1.0, Catalog v2.0,
Order v1.0 e Events v1.0. Elas permitem implementar as consultas e operações de
teste descritas abaixo. A documentação web continua respondendo HTTP403 neste
ambiente; as coleções recebidas são a referência disponível, não uma confirmação
de que todos os exemplos têm schema completo.

Continuam faltando definições para alterar horários e enviar/substituir fotos.
As quatro rotas PATCH de catálogo documentam um objeto por entidade, sem contrato
de atualização em lote exigido pelo checklist. Repetir chamadas individuais não
comprova esse requisito. Pausar um produto local não altera o catálogo ou o
funcionamento da loja no iFood.

Todos os cenários de homologação devem usar loja de teste confirmada e ambiente
isolado. Os cadastros das três lojas reais não são alvos de homologação.
Não substituir a configuração anterior do importador pelas credenciais da nova
conta sem confirmar o vínculo e preparar um perfil separado: a verificação e a
fila de eventos existentes pertencem à configuração de origem.

Os recursos implementados não equivalem a cenários homologados. Ainda é necessário
executá-los na loja de teste, conferir os resultados no Portal do Parceiro,
resolver as definições pendentes e gravar cada cenário solicitado. Nenhuma das
três lojas reais está ativa pela integração.

## Importação de teste disponível

O PDV permite verificar Client Credentials e importar novos pedidos de delivery
da loja de teste. Os pedidos aparecem em **Pedidos e cozinha**, com identificação
**iFood · recebido em teste**, seus valores originais e opção de imprimir comanda.
A indicação pública da integração continua pendente: a loja real, a homologação,
a sincronização das etapas/cancelamentos e o Entrega Fácil ainda precisam de
implementação e validação próprias.

## Configuração no serviço do PDV no Render

Use a aplicação centralizada de teste e cadastre diretamente no servidor:

| Variável | Valor |
| --- | --- |
| SAHARA_IFOOD_CLIENT_ID | clientId da aplicação centralizada |
| SAHARA_IFOOD_CLIENT_SECRET | segredo atual da aplicação, guardado somente no servidor |
| SAHARA_IFOOD_MERCHANT_ID | ID da loja de teste autorizada |
| SAHARA_IFOOD_ENABLED | 1 |
| SAHARA_IFOOD_ENVIRONMENT | test |
| SAHARA_IFOOD_IMPORT_ENABLED | 1 para consulta automática; 0 para apenas consulta manual |

O proprietário confirmou a loja de teste `42f9aefa-b035-4e01-be92-d1c9a4adf828`.
Esse ID não identifica a loja real. Um segredo apareceu em uma captura anterior;
use um segredo renovado pelo portal/suporte e nunca copie valores das capturas.
Essas variáveis pertencem ao serviço **sahara-esfihas-pdv**, não ao Evolution.
Nenhum segredo ou token é recebido ou exibido pelo painel.

Em **Integrações**, use **Testar conexão com o iFood**. A importação exige uma
verificação bem-sucedida correspondente à configuração atual. Uma alteração de
credenciais, loja ou ambiente invalida a verificação anterior. Apenas
`SAHARA_IFOOD_ENVIRONMENT=test` é aceito; a configuração production não consulta
nem altera APIs externas.

### Perfil separado da nova conta

O perfil preparatório da nova conta usa variáveis opcionais separadas:

| Variável | Valor |
| --- | --- |
| SAHARA_IFOOD_HOMOLOGATION_CLIENT_ID | clientId do Teste (C) da nova conta |
| SAHARA_IFOOD_HOMOLOGATION_CLIENT_SECRET | segredo renovado, cadastrado diretamente no Render |
| SAHARA_IFOOD_HOMOLOGATION_MERCHANT_ID | Merchant UUID da nova loja de teste, após confirmação |

O clientId confirmado pela captura é
`aceb55bd-caea-4f8a-9cd8-62adb5e40370`. O UUID da loja e um segredo renovado ainda
precisam ser configurados por um meio seguro. Não obter o segredo da imagem nem
enviá-lo pelo chat. O proprietário informou que renovou o segredo; o valor novo
precisa ser salvo com segurança no servidor. O GET de diagnóstico desse perfil é
somente local. Com clientId e segredo configurados, o botão **Consultar UUID da
loja de teste da conta nova** executa OAuth e apenas GETs de listagem/detalhes,
sem exigir que o UUID já seja conhecido. Retorna somente UUIDs com ID coincidente
e marcador explícito `test: "TEST"`; uma loja sem essa comprovação invalida o
resultado inteiro. A consulta tem paginação limitada, prazo, cooldown e bloqueio
por mudança de configuração. Não salva automaticamente o UUID nem ativa operações.

A variável opcional `SAHARA_IFOOD_HOMOLOGATION_ENVIRONMENT` aceita somente `test`
(padrão). O perfil continua bloqueado para operações de negócio. Sua ativação
exige preparar o uso independente desse perfil e validar os fluxos/filas próprios.
Ele não substitui as variáveis, a verificação ou a fila de eventos do importador
anterior. Não é um perfil de produção.

## Contratos confirmados e fluxo

A documentação do portal foi bloqueada por Cloudflare com HTTP403 no ambiente.
O proprietário forneceu referências oficiais por capturas, texto e as quatro
coleções do Postman:

- Authentication: POST form-urlencoded
  `https://merchant-api.ifood.com.br/authentication/v1.0/oauth/token`, com
  `grantType=client_credentials`, `clientId` e `clientSecret`.
- Merchant: GET `/merchant/v1.0/merchants/{merchantId}` com Bearer. O ID precisa
  corresponder à loja configurada. A autenticação e o acesso à loja de teste foram
  confirmados em requisição real no servidor em 09/10/2026, às 22h55 (GMT-3).
- Events: servidor `https://merchant-api.ifood.com.br/events/v1.0`,
  GET `/events:polling`. A lista contém `id`, `orderId`, `createdAt`, `fullCode`,
  `code` e metadata. O evento de novo pedido é `fullCode=PLACED`, `code=PLC`.
- Order: servidor `https://merchant-api.ifood.com.br/order/v1.0`,
  GET `/orders/{id}`. O proprietário forneceu o exemplo completo com itens,
  opções/customizações, totais, pagamentos, cliente, endereço e agendamento.
- Confirmação: POST `/events/acknowledgment` no servidor Events, JSON
  `[ { "id": "ID_DO_EVENTO" } ]`. Respostas HTTP200/202/204 confirmam que a
  requisição foi aceita. HTTP202 indica processamento assíncrono; não representa
  confirmação de preparo ou entrega do pedido.

A consulta usa os parâmetros padrão, sem inventar filtros obrigatórios. Se o
provedor exigir parâmetros adicionais, o painel mostra a falha e a consulta deve
ser corrigida com base nos Parameters oficiais. Não tente outro host/versão
automaticamente.

O primeiro pedido gerado no simulador oficial foi validado em 09/10/2026, às
23h38 (GMT-3): dois itens, subtotal de R$21,00 e total original de R$27,00,
com uma única ocorrência no PDV. O iFood aceitou a confirmação do evento com
HTTP202 e a pendência local foi encerrada após a gravação. A publicação da ocasião
usou a correção `a695f5f9b68a09acc1cd4439d5a341c8ab93de4d`; 25 testes de
conexão/importação passaram antes da publicação. Isso valida a entrada do pedido
de teste, sem confirmar preparo, despacho ou entrega no iFood.

Para gerar outro pedido, acesse **Pedidos de teste** no menu do iFood Developer
(`/pt-BR/developer/tests`) e clique em **Gerar pedido de teste** para a loja
de teste. A área Widgets não é o gerador de pedidos.

A consulta manual exige login, origem válida e CSRF. Abrir/atualizar o painel
consulta apenas o estado local; não importa pedidos. O botão **Buscar pedidos de
teste do iFood** executa uma consulta explícita. A consulta automática ocorre no
servidor a cada minuto, quando a flag está ativa e a conexão foi verificada;
continua funcionando com o painel fechado. Usa a mesma reserva persistente da
consulta manual, sem executar duas importações simultâneas. Há pausa de dez
minutos para respostas de limite de consultas. Reservas interrompidas expiram
após três minutos. O processo é registrado no lifespan e reiniciado com o serviço.

Cada ciclo busca até três novos detalhes para manter a duração limitada. Os demais
eventos permanecem para outra consulta. Processa apenas PLACED de pedidos de
delivery da loja configurada. Eventos de outras etapas, payloads inválidos,
pedidos de outra loja ou tipos ainda não suportados não são confirmados. Os
contadores de pendências/falhas ficam visíveis no painel.

O pedido e seu evento são gravados na mesma transação SQLite. A associação única
entre loja/ID externo e pedido local evita duplicações, inclusive quando eventos
com IDs distintos referenciam o mesmo pedido. A confirmação de até cem eventos
persistidos ocorre somente após commit. Se falhar ou o servidor reiniciar, a
confirmação é tentada em outro ciclo, inclusive quando o polling estiver vazio.
Não é necessário importar novamente o pedido ou obter seus detalhes novamente.

## Valores e operação

O total usa `total.orderAmount` do iFood; subtotal, benefícios, taxas, total de
cada item, quantidades fracionárias, opções, customizações e observações são
preservados. Valores monetários finitos e não negativos são convertidos em
centavos usando Decimal; valores com frações de centavo são rejeitados. Não há
recalculo pelo cardápio local, aplicação de cupons locais ou substituição por
preços locais. O agendamento usa a janela original do iFood.

Nome, telefone/localizador, endereço, referência, cidade, estado, CEP e coordenadas
são exibidos apenas no painel privado. O localizador de telefone do iFood não cria
um contato de CRM/marketing. Pagamento antecipado e pendente são apresentados
conforme o iFood, sem gerar recebimento no caixa local. Não há baixa de estoque,
fidelidade, fiado, campanha ou mensagem de WhatsApp para pedidos de teste.

As etapas locais, pagamento, fiado e atribuição de entregador são bloqueados para
pedidos importados, inclusive por API. Os comandos manuais de teste descritos
abaixo enviam solicitações ao iFood e não mudam essas etapas locais.
Confira os resultados no Gestor do iFood. Não habilite webhook antes de existir
um receptor validado. Para sincronizar as etapas, ainda faltam os contratos
dos eventos de ciclo de vida e suas transições, incluindo cancelamento,
entrega, erros e reembolso. A coleção recebida não define uma ação genérica de
pedido entregue nem o mapeamento completo de códigos dos eventos.

## Transporte e validação

Host fixo HTTPS, proxy/TLS preservados, sem redirecionamentos, timeout de oito
segundos por requisição. Respostas de eventos/detalhes limitadas a 1MiB; tokens e
corpos de erros do provedor não são persistidos, retornados ou escritos em logs.
Somente dados necessários do pedido e IDs dos eventos são guardados no banco
privado, fora da pasta publicada. O ciclo usa token efêmero, sem gravá-lo em disco.

## Consulta Merchant da loja de teste

Em **Integrações → iFood → Informações e disponibilidade da loja de teste**,
o botão **Consultar lojas e disponibilidade** faz uma consulta explícita da lista
de lojas vinculadas, dos detalhes da loja configurada e da disponibilidade
retornada pelo iFood. O painel mostra client_id de teste, loja e data da consulta.
Os dados detalhados e a resposta de disponibilidade aparecem em seções expansíveis
privadas, como texto; uma falha não é apresentada como loja aberta ou fechada.

GET `/api/admin/ifood/merchant` lê somente a prontidão local. POST na mesma rota,
com objeto vazio, executa a consulta e exige login, origem válida e CSRF. O alvo
vem do servidor e deve ser a loja de teste previamente confirmada, com ambiente
`test` e diagnóstico de conexão verificado para a configuração atual. Não há
seleção de loja real ou de outro destino no navegador. A consulta tem reserva
persistente e intervalo mínimo de 30 segundos; uma execução interrompida libera
a reserva após três minutos.

As operações externas desta consulta são a autenticação já existente e os
GET de `/merchant/v1.0/merchants`, `/merchant/v1.0/merchants/{merchantId}` e
`/merchant/v1.0/merchants/{merchantId}/status`. A lista segue a paginação oficial,
com até três páginas de cem lojas; atingir esse limite sem concluir a lista é
apresentado como falha, não como resultado completo. A consulta não escreve no
iFood; as operações manuais de teste têm controles próprios, descritos abaixo.
Segredos são removidos da resposta;
somente o estado seguro da consulta é persistido, sem armazenar a lista ou os
detalhes completos da loja. O host, TLS e bloqueio de redirecionamento do cliente
existente são preservados.

A função usa a configuração anterior de teste que está no Render. Ela não
comprova vínculo dos aplicativos da nova conta, autorização das três lojas reais
ou aprovação na homologação. A execução remota precisa ser conferida no ambiente
configurado; testes com provedor simulado não substituem essa conferência nem os
vídeos com validação no Portal do Parceiro.

## Operações manuais de homologação implementadas

As áreas privadas de Merchant e Catalog aparecem em **Integrações**. Abrir o
painel lê somente o estado local; as consultas ao provedor e as escritas exigem
uma ação explícita. Todas usam login administrativo, origem válida, CSRF,
ambiente `test` e conexão verificada para a configuração atual. O alvo é fixado
no servidor: a loja confirmada `42f9aefa-b035-4e01-be92-d1c9a4adf828`. O navegador
não escolhe um merchant de produção nem usa o perfil ainda bloqueado da nova conta.

Cada escrita registra sua intenção antes da chamada externa. Respostas incertas
bloqueiam novas escritas conflitantes. Para pausas e catálogo, a reconciliação
consulta o resultado sem reenviar a criação. A propriedade dos recursos é ligada ao aplicativo e à loja,
preservando as pendências quando o segredo da mesma conexão é renovado. A
verificação da conexão, por sua vez, exige as credenciais atuais.

### Merchant: pausas e leitura de horários

É possível consultar interrupções, criar uma pausa com descrição, início e fim
com fuso horário, remover uma pausa criada pelo sistema e reconciliar seu
resultado por consulta. A remoção usa a operação local registrada; não aceita
um ID remoto livre para apagar pausas de outra origem. O iFood atribui seu próprio
ID à pausa. Novas criações incluem um marcador exclusivo na descrição para
reconciliar uma resposta perdida; múltiplos resultados correspondentes mantêm o
bloqueio. O retorno observado omite o fuso e arredonda datas para milissegundos:
o sistema compara com a intenção UTC, conserva o retorno original e usa o fuso
conhecido da própria intenção na exibição. Intenções da versão anterior só são
reconciliadas manualmente por descrição e intervalo exatamente correspondentes,
com resultado único. Uma confirmação pela API
ainda deve ser conferida no Portal do Parceiro para o vídeo.

GET `/merchant/v1.0/merchants/{merchantId}/opening-hours` permite consultar os
horários. A coleção também apresenta uma rota de escrita, mas seus exemplos não
definem a unidade de `duration`, o formato de `start`, os valores de `dayOfWeek`
ou a semântica de `storeId`. A alteração de horários permanece bloqueada até
obter essas definições oficiais.

### Catalog: recursos controlados e alterações individuais

O painel descobre catálogos da loja de teste e consulta suas categorias e itens.
A partir de modelos válidos efetivamente consultados, pode criar uma categoria
**Teste Homologação**, um **Produto Teste** simples e um grupo com dois
complementos. IDs novos e associações registradas separam os recursos criados
dos modelos existentes. Códigos de status, tipo e template são aproveitados dos
modelos, sem inventar enums ausentes na coleção.

A criação do produto e dos complementos **reutiliza fotos de modelos existentes**.
Isso não realiza upload e não conclui os cenários de envio ou substituição de
imagens. A coleção descreve `/image/upload` apenas como `<object>`, sem schema
do arquivo ou da resposta; essas funções permanecem bloqueadas.

As rotas `/items/price`, `/items/status`, `/options/price` e `/options/status`
enviam um PATCH por entidade criada e registrada pelo sistema. Os preços usam
centavos inteiros; os status precisam ter sido observados no catálogo. O sistema
consulta o recurso após a escrita para conferir o resultado. Operações em curso
ou incertas mantêm a reserva até reconciliação, evitando novas alterações antes
de conhecer seu resultado. Atualizações em lote permanecem bloqueadas porque
não há contrato de lote nas coleções recebidas.

Essas operações não copiam os 57 produtos do site e não alteram os cadastros ou
catálogos das três lojas reais.

### Order: solicitações manuais para pedidos importados de teste

Cada cartão de pedido de teste elegível pode solicitar `confirm`,
`startPreparation`, `readyToPickup`, `dispatch` ou `requestCancellation`.
O ID externo vem exclusivamente do mapeamento privado do importador. Antes de
cada comando, o servidor consulta os detalhes atuais e confere ID, loja e tipo
delivery. O cancelamento exige consultar os motivos atuais e selecionar um
código retornado pelo iFood, acompanhado de justificativa.

HTTP202 é apresentado como **solicitação aceita, etapa pendente de confirmação**.
Não marca o pedido como preparado, despachado, entregue ou cancelado no PDV e não
altera estoque, caixa, pagamentos ou WhatsApp. A mesma solicitação não é reenviada
quando já foi aceita; uma tentativa incerta bloqueia novos comandos para esse
pedido. A atualização automática das etapas depende dos contratos de Events que
ainda faltam. Nenhum desses comandos constitui aprovação na homologação.

O transporte dessas operações limita a resposta a 2MiB, usa host HTTPS fixo,
proxy/TLS preservados, token efêmero e timeout de oito segundos por requisição.
Não segue redirecionamentos e só aceita os métodos e caminhos previstos. Tokens,
segredos e corpos de erros do provedor não são gravados no histórico das operações.

Testes locais exercitam provedores simulados, banco temporário e credenciais
fictícias. Cobrem preços distintos do catálogo, adicionais, pagamentos, janelas
agendadas, duplicação, confirmação após commit, recuperação de falhas, concorrência,
controle de acesso, loja divergente, isolamento financeiro e execução automática
sem painel aberto. Não comprovam homologação nem importação de pedido real.

O Render usa deploy manual. Push no GitHub, deploy Live, consulta real de eventos
e importação de um pedido de teste são verificações distintas.
