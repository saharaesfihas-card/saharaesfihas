# WhatsApp da Sahara por QR Code — Evolution API

O PDV está preparado para conectar ao WhatsApp Business do celular pela
**Evolution API v2, usando Baileys/WhatsApp Web**. Este é o caminho solicitado,
sem ativar a WhatsApp Cloud API da Meta. O aplicativo continua no celular, e
você vincula a sessão como um aparelho conectado.

O código está salvo no repositório. A ativação no Render e o pareamento do número
dependem dos passos abaixo. Nenhum número real foi pareado nesta preparação.
Baileys usa o WhatsApp Web e não é a API oficial da Meta; essa conexão pode exigir
novo pareamento e manutenção quando o WhatsApp mudar.

## O que está disponível

O robô também pode interpretar dúvidas de delivery com a cota gratuita do Gemini.
A ativação é opcional e está descrita em [WHATSAPP-IA.md](WHATSAPP-IA.md).

- **Integrações → WhatsApp da Sahara → Gerar QR Code**: cria a instância quando
  necessário, configura os eventos e mostra o QR Code no painel privado.
- Consulta real da conexão: conectado, desconectado, aguardando pareamento ou
  indisponível. A configuração presente não é anunciada como conexão ativa.
- Menu automático: cardápio, consultar pedido, horários, pagamentos e atendente.
- Novos pedidos são finalizados no cardápio existente e registrados no PDV.
  O robô não transforma pedidos em texto livre ou áudio em pedidos completos.
- Avisos de preparo, pronto, a caminho, entregue e cancelado, com autorização
  específica do cliente. Não são exigidos os modelos da Cloud API para este
  transporte; a autorização do cliente continua necessária.
- `PARAR` desativa avisos; `ATIVAR AVISOS` autoriza os pedidos ativos do número.
  A autorização para promoções é separada; campanhas continuam como rascunhos.
- `ATENDENTE` pausa o robô nessa conversa por 24 horas. A equipe pode responder
  pelo PDV. `MENU` ou **Retomar robô** reativa o atendimento automático.
- Mensagens enviadas pelo próprio WhatsApp, grupos e broadcasts não acionam o
  robô. Histórico antigo sincronizado no pareamento não recebe respostas.
- Áudios e outros conteúdos não textuais seguem para a equipe; não há transcrição.
- O histórico mostra fila, aceite pelo serviço, envio, entrega, leitura e falha.
  Aceite não significa entrega. As confirmações dependem dos eventos do WhatsApp.

## 1. Publicar a atualização do PDV

O repositório conectado ao Render é:

`https://github.com/saharaesfihas-card/saharaesfihas`

Use **Manual Deploy → Deploy latest commit**
no serviço `sahara-esfihas-pdv`. A conexão permanece desligada até configurar as
variáveis abaixo. O banco é atualizado automaticamente, sem apagar os pedidos.
Pedidos antigos permanecem sem autorização de avisos; não há disparo retroativo.

## 2. Hospedar a Evolution API

Ela precisa de um serviço separado e contínuo. Pode usar uma Evolution API v2
já contratada ou hospedada, desde que permita configurar webhooks com cabeçalhos
privados. Nesse caso, pule para o passo 3.

Para hospedar no Render, foi preparado **render.evolution.yaml**, um Blueprint
separado do atual PDV. Ele define:

- Evolution API `evoapicloud/evolution-api:v2.3.7`, com imagem fixada pelo digest
  verificado no registro oficial, 2 GB de RAM e disco persistente.
- PostgreSQL para dados e credenciais da sessão da Evolution API, separado do
  banco de pedidos do PDV.
- Key Value compatível com Redis para cache. As credenciais da sessão ficam no
  PostgreSQL, com `CACHE_REDIS_SAVE_INSTANCES=false`.

**Os três recursos e o disco geram custos adicionais.** O Render mostra os
valores antes da criação; confira-os na conta da Sahara. Nada foi criado durante
esta preparação. O plano gratuito, que pode suspender o serviço, não foi usado
como base para um atendimento contínuo.

No Render, crie um novo Blueprint a partir do repositório, selecionando o arquivo
`render.evolution.yaml`. Não substitua o Blueprint `render.yaml` do PDV por este.
Aguarde os três recursos ficarem disponíveis. O nome sugerido do novo serviço é
`sahara-whatsapp-evolution`; use a URL HTTPS realmente atribuída pelo Render,
que pode diferir do nome sugerido.

A chave `AUTHENTICATION_API_KEY` da Evolution é gerada pelo Blueprint. Copie-a
privadamente em **Environment** para a configuração do PDV. Ela não deve ser
publicada no repositório nem enviada no chat. O PDV gera e exibe o QR Code, portanto
não é necessário hospedar também o Evolution Manager para esse fluxo.

O Render fornece HTTPS para o serviço. A Evolution escuta internamente na porta
8080; PostgreSQL e Redis usam conexões privadas e não liberam acesso externo.
O servidor do PDV envia a chave no cabeçalho `apikey`. O navegador acessa apenas
o PDV. O Blueprint restringe CORS ao endereço do PDV, desativa o Evolution Manager
e usa autenticação por chave. A listagem autenticada da Evolution pode conter
credenciais da instância; o PDV não as envia ao navegador. Mantenha o acesso ao
painel e aos logs do serviço limitado à equipe: a Evolution 2.3.7 pode registrar
conteúdo recebido em seus logs, mesmo com `LOG_LEVEL=ERROR,WARN`.

O Blueprint usa a verificação TCP do Render, sem `healthCheckPath` HTTP. Na
Evolution 2.3.7, CORS restrito recusa requisições sem `Origin`, e a rota `/` também
consulta a versão do WhatsApp pela internet. Por isso, confirme a prontidão pelo
PDV, em **Verificar conexão**, depois de as migrações do PostgreSQL terminarem.
O cliente do PDV envia o `Origin` correspondente a `SAHARA_PUBLIC_URL`; mantenha
esse endereço igual ao `CORS_ORIGIN` configurado na Evolution.

Se usar outro serviço/provedor, mantenha PostgreSQL, armazenamento da sessão e
cache conforme a versão instalada. A adaptação foi preparada para Evolution API
**v2**; Evolution Go e outros produtos têm contratos diferentes.

## 3. Configurar Environment no PDV

No serviço **sahara-esfihas-pdv**, configure:

| Variável | Valor |
| --- | --- |
| `SAHARA_WHATSAPP_ENABLED` | `1` para ativar; `0` para pausar |
| `SAHARA_WHATSAPP_PROVIDER` | `evolution` |
| `SAHARA_EVOLUTION_URL` | URL HTTPS do serviço Evolution, sem `/manager`, usuário ou senha na URL |
| `SAHARA_EVOLUTION_API_KEY` | A chave privada `AUTHENTICATION_API_KEY` do serviço Evolution |
| `SAHARA_EVOLUTION_INSTANCE` | `sahara` |
| `SAHARA_EVOLUTION_EXPECTED_NUMBER` | `5544991748318`, número da loja, com país e DDD e somente dígitos |
| `SAHARA_EVOLUTION_WEBHOOK_SECRET` | Segredo aleatório privado, diferente da chave da API |
| `SAHARA_PUBLIC_URL` | `https://sahara-esfihas-pdv.onrender.com` |

Para gerar o segredo de webhook no computador, pode usar:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Copie o valor somente para o campo privado do Render. O arquivo
`.env.whatsapp.example` contém os nomes de configuração e valores vazios.

A Evolution precisa alcançar a URL pública HTTPS do PDV. Se houver regras de
rede no provedor, permita essa comunicação e o acesso da Evolution ao WhatsApp.
Mantenha o HTTPS ativo. As credenciais da Meta não são necessárias para o modo
`evolution`; a alternativa oficial continua disponível em **WHATSAPP-META.md**.

## 4. Parear o número

1. Abra `https://sahara-esfihas-pdv.onrender.com/admin.html` e entre na gestão.
2. Vá a **Integrações** e confira **WhatsApp da Sahara**.
3. Toque em **Gerar QR Code**. O PDV cria a instância `sahara`, se necessário,
   e configura o webhook autenticado antes de gerar o código.
4. No WhatsApp Business **da loja**, abra **Aparelhos conectados → Conectar um
   aparelho** e escaneie o QR Code exibido no painel.
5. Se o painel estiver no mesmo celular do WhatsApp, abra o PDV em um computador
   ou em outro aparelho para conseguir escanear a tela.
6. Use **Atualizar dados** no PDV e confira **WhatsApp: Conectado**. Se o QR
   expirar ou não estiver pronto, toque em **Gerar QR Code** novamente.

O QR Code é uma credencial de pareamento: não compartilhe com terceiros.
O PDV não envia a chave da Evolution nem o segredo de webhook ao navegador.
**Verificar conexão** também reaplica a configuração de webhook. Use esse botão
após reinício ou alteração na Evolution, especialmente se os eventos pararem.

## 5. Testar antes de usar na operação

Use outro número de celular como cliente:

1. Envie `Olá` e confira o menu automático. Teste `1`, `3` e `4`.
2. Pelo cardápio, finalize um pedido informando o número desse cliente e marcando
   **Quero receber avisos deste pedido no WhatsApp**.
3. Envie `2` no WhatsApp. O robô deve consultar apenas pedidos do número remetente.
4. No PDV, mova o pedido para **Pronto** e **A caminho**. Confira os avisos no
   celular do cliente e atualize o histórico de mensagens.
5. Envie `ATENDENTE`, responda pelo PDV e confirme a pausa do robô. Use `MENU`
   para retornar. A pausa permite atendimento pela equipe também no aplicativo.
6. Envie `PARAR` e confira que as novas etapas não geram avisos. Use `ATIVAR
   AVISOS` para autorizar novamente os pedidos ativos.

Não foi enviado nenhum WhatsApp real nos testes de desenvolvimento. O pareamento
real, a estabilidade do número e a entrega precisam desse teste na conta da loja.

## Eventos e proteção de dados

O PDV configura automaticamente o webhook:

`https://sahara-esfihas-pdv.onrender.com/api/whatsapp/evolution/webhook`

Ele usa `X-Sahara-Webhook-Secret`, com o segredo privado do Render, e aceita
somente a instância configurada. `byEvents=false`, `base64=false`, e eventos:
`MESSAGES_UPSERT`, `MESSAGES_UPDATE`, `CONNECTION_UPDATE`.

O contrato foi conferido no código oficial da versão 2.3.7: o PDV configura
`POST /webhook/set/sahara` com `{ "webhook": { "enabled": true, "url": "...",
"headers": { "X-Sahara-Webhook-Secret": "..." }, "byEvents": false,
"base64": false, "events": ["MESSAGES_UPSERT", "MESSAGES_UPDATE",
"CONNECTION_UPDATE"] } }`. O envio de texto usa `POST /message/sendText/sahara`
com `{ "number": "55...", "text": "..." }` e o cabeçalho privado `apikey`.
Não use chaves reais em exemplos públicos ou comandos compartilhados.

A identidade do cliente vem do endereço telefônico da sessão. IDs `@lid` só
podem ser usados quando o evento fornece também o endereço telefônico
`remoteJidAlt`; um LID isolado não é tratado como telefone. A consulta não
confia no telefone escrito no texto da mensagem. Mensagens repetidas não
provocam novas respostas, e confirmações fora de ordem não reduzem o estado
já confirmado de entrega ou leitura.

## Fila, reconexão e falhas

A fila fica no disco SQLite privado do PDV. O servidor só consome a fila
Evolution depois de consultar a conexão e confirmar que ela pertence ao número
da loja. Essa confirmação vale por até 30 segundos e é invalidada quando a
configuração muda. Enquanto desconectada, as mensagens ficam pendentes.
Os eventos de pedidos e seus avisos são gravados na mesma transação.

Falhas de rede, respostas ambíguas e reinício durante envio podem deixar um
resultado **Sem confirmação**. Confira a conversa antes de enviar outra mensagem;
nesses casos não há reenvio automático. HTTP 429 tem até cinco tentativas com
espera crescente. Falhas explícitas têm reenvio manual após corrigir a causa.

`SAHARA_WHATSAPP_ENABLED=0` pausa envios e respostas e não gera novos avisos
nas mudanças de etapa. A fila existente é preservada. Ao reconectar, avisos de
etapas já superadas são descartados; o painel preserva esse resultado no histórico.

As mensagens da fila são associadas ao provedor que as criou. Trocar entre Meta
e Evolution não transfere mensagens pendentes de uma conexão para outra.
Os avisos já autorizados de clientes que enviarem `PARAR` são descartados antes
de enviar. Campanhas não são disparadas por esta integração.

O limite do PDV é 64 KiB por evento; o webhook é configurado sem mídias em
base64. Mensagens maiores exigem revisar o limite antes de uso.

## Verificação de desenvolvimento

O painel carrega a disponibilidade dos serviços antes de consultar a conexão do
WhatsApp. Uma falha nessa consulta fica restrita à seção do WhatsApp, com a opção
**Tentar novamente**; os demais módulos continuam acessíveis. As consultas do
PDV têm limite de 20 segundos, a consulta da Evolution de 35 segundos e o
pareamento de 100 segundos, pois pode fazer várias chamadas ao provedor.
O limite inclui a leitura da resposta. Operações não são repetidas automaticamente
após uma interrupção: confira o resultado antes de tentar novamente, principalmente
ao registrar pedidos, pagamentos ou mensagens.

```sh
python -m unittest discover -s server/tests -v
node tests/admin.spec.cjs
node tests/evolution-admin.spec.cjs
node tests/ordering.spec.cjs
```

Os testes de transporte e pareamento são simulados, sem número real conectado.
O Blueprint foi validado com o schema oficial do Render; o serviço não foi
implantado nesta sessão. A imagem oficial 2.3.7 foi baixada com verificação de
TLS e digest e executada em contêineres isolados com PostgreSQL 15 e Redis 7.2.
As migrações concluíram, os testes de chave e CORS passaram e uma instância local
sem pareamento confirmou o formato de identidade e os cabeçalhos do webhook.
Essa instância foi excluída depois do teste. Não houve conexão com um número nem
envio de mensagens. Atualizações futuras da Evolution/Baileys ou do WhatsApp podem
exigir manutenção.

Referências: [Evolution API](https://github.com/evolution-foundation/evolution-api),
[versão 2.3.7](https://github.com/evolution-foundation/evolution-api/releases/tag/2.3.7),
[imagem oficial](https://hub.docker.com/r/evoapicloud/evolution-api),
[Blueprints do Render](https://render.com/docs/blueprint-spec).
