# iFood: conexão e importação da loja de teste

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

## Contratos confirmados e fluxo

A documentação do portal foi bloqueada por Cloudflare com HTTP403 no ambiente.
O proprietário forneceu as referências oficiais por capturas e texto:

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
  `[ { "id": "ID_DO_EVENTO" } ]`. Apenas respostas HTTP200/204 confirmam envio.

A consulta usa os parâmetros padrão, sem inventar filtros obrigatórios. Se o
provedor exigir parâmetros adicionais, o painel mostra a falha e a consulta deve
ser corrigida com base nos Parameters oficiais. Não tente outro host/versão
automaticamente. Ainda falta validação com um pedido criado no simulador oficial.

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
pedidos importados, inclusive por API. Confira/gerencie esses pedidos no Gestor
do iFood. Não habilite webhook antes de existir um receptor validado. Para
integrar as quatro etapas, obtenha os contratos oficiais dos eventos de mudança
e das ações de confirmação/preparo/despacho/entrega/cancelamento, incluindo
transições, erros, reembolso e critérios de homologação.

## Transporte e validação

Host fixo HTTPS, proxy/TLS preservados, sem redirecionamentos, timeout de oito
segundos por requisição. Respostas de eventos/detalhes limitadas a 1MiB; tokens e
corpos de erros do provedor não são persistidos, retornados ou escritos em logs.
Somente dados necessários do pedido e IDs dos eventos são guardados no banco
privado, fora da pasta publicada. O ciclo usa token efêmero, sem gravá-lo em disco.

Testes locais exercitam provedores simulados, banco temporário e credenciais
fictícias. Cobrem preços distintos do catálogo, adicionais, pagamentos, janelas
agendadas, duplicação, confirmação após commit, recuperação de falhas, concorrência,
controle de acesso, loja divergente, isolamento financeiro e execução automática
sem painel aberto. Não comprovam homologação nem importação de pedido real.

O Render usa deploy manual. Push no GitHub, deploy Live, consulta real de eventos
e importação de um pedido de teste são verificações distintas.
