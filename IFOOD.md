# iFood: primeira etapa de conexão

O painel Integrações permite consultar a configuração e testar a autenticação
Client Credentials e o acesso a uma loja de teste. Não importa pedidos,
confirma pedidos, muda etapas no iFood nem solicita entregadores.
O indicador público de integração permanece pendente.

## Configuração no serviço do PDV no Render

Use a aplicação centralizada de teste e cadastre diretamente no servidor:

| Variável | Valor |
| --- | --- |
| SAHARA_IFOOD_CLIENT_ID | clientId da aplicação centralizada |
| SAHARA_IFOOD_CLIENT_SECRET | segredo atual da aplicação, guardado somente no servidor |
| SAHARA_IFOOD_MERCHANT_ID | ID da loja autorizada, diferente do clientId |
| SAHARA_IFOOD_ENABLED | 1 |
| SAHARA_IFOOD_ENVIRONMENT | test |

O proprietário confirmou como ID da loja de teste:
`42f9aefa-b035-4e01-be92-d1c9a4adf828`.
Não use esse ID como identificação da loja real. A aplicação exibida nas capturas
é de teste, com Order, Events e Merchant autorizados.
Um segredo apareceu em uma captura do chat. Solicite sua substituição no portal
ou ao suporte antes de cadastrar o novo valor no Render. Não copie o segredo
da captura para código, testes, arquivos ou instruções salvas.

Após implantar o código no serviço **sahara-esfihas-pdv**, abra Integrações,
localize **iFood · conexão com a loja de teste** e toque em **Testar conexão com o iFood**.
O POST privado exige login e CSRF; a configuração usa GET privado. Nenhum campo
do painel recebe ou mostra o segredo ou token do iFood.

O teste faz POST form-urlencoded em
`https://merchant-api.ifood.com.br/authentication/v1.0/oauth/token`
com `grantType=client_credentials`, `clientId` e `clientSecret`, seguido de
GET `/merchant/v1.0/merchants/{merchantId}` com Bearer. O ID retornado precisa
corresponder à loja configurada. Token usado apenas durante a requisição;
nenhum token ou corpo bruto do provedor é persistido ou retornado ao painel.
Host fixo, TLS e proxy preservados, redirecionamentos bloqueados, timeout de
8 segundos por consulta e resposta limitada a 64 KiB. Há um intervalo global
persistente de 30 segundos entre testes. Não há repetição automática.
A indicação de teste anterior é invalidada por mudança de configuração.

## Validação e etapas pendentes

Os testes locais usam provedor simulado, SQLite temporário e credenciais
fictícias. Não comprovam autenticação real, homologação ou recebimento de
pedidos. A documentação oficial no developer.ifood.com.br ficou bloqueada pelo
proxy do ambiente. O contrato das consultas precisa ser confirmado no teste
real e na documentação oficial; não altere automaticamente o tipo de aplicação
ou tente outros fluxos OAuth após uma falha.

Antes de adicionar importação e comandos de pedidos, confirme na documentação
oficial os contratos atuais de Order e Events, cenários e critérios de
homologação. Prepare tratamento de duplicidade, confirmação de processamento
dos eventos, valores e adicionais do iFood, pedidos agendados, cancelamentos,
conciliação e transições permitidas. Não use preços do cardápio local para
recalcular pedidos recebidos do iFood. Não habilite o webhook até existir um
endpoint validado. Pedidos reais exigem autorização da loja real e liberação
apropriada da aplicação pelo iFood.

O Render do PDV usa deploy manual. Push no GitHub, deploy Live e teste real
são três verificações diferentes. Esta primeira etapa não conclui a integração.
