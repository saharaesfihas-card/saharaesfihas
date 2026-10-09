# WhatsApp da Sahara

Esta integração usa a **WhatsApp Business Platform / Cloud API oficial da Meta**.
O aplicativo WhatsApp Business no celular, sozinho, não fornece a conexão com o PDV.
Não utiliza leitura de QR code, automação do WhatsApp Web nem serviços não oficiais.

## O que foi preparado

- Menu automático: cardápio, andamento do pedido, horários, pagamentos e atendente.
- O cliente escolhe produtos e finaliza no cardápio existente; o checkout registra o
  pedido no PDV. O robô não interpreta texto livre para criar pedidos ou cobrar pagamentos.
- Consulta de pedido vinculada ao número autenticado pela Meta, sem confiar no
  telefone ou ID informado na mensagem.
- Avisos de preparo, pronto, a caminho, entregue e cancelado. É preciso marcar a
  autorização de avisos ao finalizar o pedido ou registrá-la no PDV. O cliente pode
  enviar `ATIVAR AVISOS` para autorizar seus pedidos ativos, ou `PARAR` para desativar.
- Transferência para a equipe: `ATENDENTE` pausa o robô por 24 horas; a equipe vê a
  conversa e responde em **Integrações → Atendimento pelo WhatsApp**. `MENU` ou
  **Retomar robô** volta ao atendimento automático.
- Histórico com fila, aceite pela API, envio, entrega, leitura e erros. Aceite pela
  API não significa entrega. Use **Atualizar dados** para consultar novos eventos.

Campanhas promocionais continuam como rascunhos. A autorização para ofertas é
separada da autorização para avisos de pedidos. IA generativa não foi adicionada:
as respostas usam regras e dados reais do PDV.

## 1. Habilitar o número na Meta

Na conta da Sahara, configure um app empresarial com WhatsApp, a conta WhatsApp
Business e o número que enviará as mensagens. Para os primeiros testes, pode usar
o número de teste da Meta e cadastrar os destinatários de teste autorizados.

Como o número atual usa o aplicativo no celular, confirme com a Meta ou seu
provedor se a conta e o número são elegíveis ao uso simultâneo do aplicativo e da
API (coexistência). Caso contrário, o provedor orientará a migração ou o uso de um
número separado. **Não exclua a conta do aplicativo para tentar ativar a integração.**

Tenha o **Phone Number ID** (ID técnico, não o telefone), **App Secret** e um token
de acesso com autorização `whatsapp_business_messaging` para o número. Para
produção, configure o token apropriado ao app e à conta; o token temporário de
teste expira. Mantenha os segredos no Render, nunca no GitHub ou no chat.

## 2. Publicar o código

Publique as alterações no repositório conectado ao serviço Render. Preserve os
demais arquivos, o disco persistente, a senha de gestão e `SAHARA_DATA_DIR`.
O banco recebe tabelas de WhatsApp e uma coluna de autorização de avisos por
pedido automaticamente; pedidos existentes permanecem sem autorização.

O Blueprint atual usa implantação manual. Depois de atualizar o repositório,
no Render use **Manual Deploy → Deploy latest commit**. As credenciais abaixo
podem ser configuradas com a integração desligada (`SAHARA_WHATSAPP_ENABLED=0`).

## 3. Configurar Environment no Render

No serviço `sahara-esfihas-pdv`, adicione:

| Variável | Valor |
| --- | --- |
| `SAHARA_WHATSAPP_ENABLED` | `1` para ativar; `0` para desligar sem apagar os dados |
| `SAHARA_WHATSAPP_ACCESS_TOKEN` | Token privado com acesso ao número |
| `SAHARA_WHATSAPP_PHONE_NUMBER_ID` | ID técnico do número na Meta |
| `SAHARA_WHATSAPP_APP_SECRET` | Segredo privado do app Meta, usado para autenticar eventos |
| `SAHARA_WHATSAPP_VERIFY_TOKEN` | Segredo aleatório criado por você e repetido na configuração do webhook |
| `SAHARA_WHATSAPP_API_VERSION` | Versão Graph API suportada pelo app; padrão `v25.0` |
| `SAHARA_PUBLIC_URL` | `https://sahara-esfihas-pdv.onrender.com` |
| `SAHARA_WHATSAPP_ORDER_TEMPLATE` | Nome exato do modelo aprovado, por exemplo `sahara_pedido_status` |
| `SAHARA_WHATSAPP_TEMPLATE_LANGUAGE` | Idioma aprovado do modelo, por exemplo `pt_BR` |

Não use o telefone comum no campo Phone Number ID. A disponibilidade exibida no
painel significa que a configuração está presente; não testa a validade do token.
A confirmação de funcionamento vem dos eventos e das mensagens reais.

## 4. Webhook na Meta

Configure a URL de retorno:

`https://sahara-esfihas-pdv.onrender.com/api/whatsapp/webhook`

Use o mesmo valor de `SAHARA_WHATSAPP_VERIFY_TOKEN`. Após a verificação, assine o
campo **messages** e confirme que o app está inscrito na conta WhatsApp Business.
O servidor devolve o desafio GET como texto e verifica a assinatura HMAC-SHA256
de cada evento POST com o App Secret. Ignora eventos de outros Phone Number IDs.

## 5. Modelo de aviso de pedido

Crie e submeta à aprovação da Meta um modelo de utilidade, sem cabeçalho, rodapé
ou botões obrigatórios, com dois parâmetros **posicionais** no corpo:

> Sahara: seu pedido #{{1}} está {{2}}.

Exemplos para aprovação: parâmetro 1 `pedido-123`, parâmetro 2 `em preparo`.
O nome e o idioma cadastrados devem coincidir com as variáveis do Render.
Usa o ID real do PDV e a etapa correspondente a cada evento.

Dentro das 24 horas após a última mensagem do cliente, as respostas e os avisos
podem usar texto livre. Fora dessa janela, os avisos de pedido usam o modelo.
Sem modelo, ficam bloqueados, com o motivo no histórico. Depois de aprovar e
configurar o modelo, use **Tentar novamente** nas mensagens bloqueadas pertinentes.
Modelos diferentes, com parâmetros nomeados ou componentes adicionais, exigem
adaptação do código antes de enviar.

## 6. Teste de ativação

1. Abra o PDV, entre na gestão e vá a **Integrações**. Confira a configuração e a
   data de verificação do webhook.
2. Pelo celular de um cliente de teste autorizado, envie `Olá` para o número da
   API. Confira as opções e responda `1`, `3` e `4`.
3. Finalize um pedido no cardápio com o mesmo número de WhatsApp e marque a
   autorização de avisos. Envie `2` no WhatsApp e confira o pedido correto.
4. No PDV, mova o pedido para **Pronto** e **A caminho**. Confira o recebimento no
   celular e o histórico de envio; atualize o painel para ver entrega e leitura.
5. Envie `ATENDENTE`, responda pelo PDV e confirme que o robô fica pausado. Envie
   `MENU` para voltar.
6. Envie `PARAR` e confira que as próximas etapas não geram novos avisos. Para
   testar fora de 24 horas, use um destinatário com autorização e modelo aprovado.

## Operação e falhas

A fila fica no mesmo SQLite privado, em disco persistente, e é consumida pelo
worker do servidor. Os avisos entram na fila na mesma transação do pedido ou da
mudança de etapa. Uma falha na Meta não cancela o pedido nem altera caixa ou estoque.
Webhooks repetidos não geram novas respostas; uma etapa repetida não duplica avisos.
Eventos de mensagem anteriores à janela de atendimento não provocam resposta.

Rejeições HTTP 429 recebem até cinco tentativas com espera crescente. Erros de
configuração ou entrega ficam visíveis para correção e reenvio manual. Falhas de
rede, HTTP 5xx e reinício durante envio podem deixar o resultado incerto: confira
a conversa e a Meta antes de enviar outra mensagem, para evitar duplicidade.
Mensagens sem confirmação não têm reenvio automático.

Desligar a integração preserva a fila, mas não gera avisos novos durante a pausa.
Pedidos sem telefone ou autorização não recebem avisos. O sistema não faz envio
retroativo de todos os pedidos antigos ao ativar. Os webhooks de WhatsApp têm limite
de 64 KiB por evento; eventos maiores precisam de revisão do limite antes do uso.

## Verificação local

```sh
python -m unittest discover -s server/tests -v
node tests/admin.spec.cjs
node tests/ordering.spec.cjs
```

Os testes de WhatsApp simulam o transporte; não enviam mensagens nem comprovam
autorização da conta Meta. O teste real exige configurar e ativar o número.

Referências oficiais: [WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api/),
[validação de webhooks da Meta](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/).
