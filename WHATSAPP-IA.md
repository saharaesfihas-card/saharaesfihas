# IA para delivery no WhatsApp da Sahara

O robô conectado por QR Code pode usar um modelo de texto **Gemini Flash-Lite** para
interpretar mensagens em linguagem natural e selecionar sugestões do cardápio.
O modelo retorna a intenção, IDs de produtos e uma resposta natural. Perguntas
gerais também recebem respostas, sem repetir o menu. Nomes e preços são marcadores
substituídos pelo PDV com os dados atuais; valores monetários e links livres são
recusados. As respostas oficiais de horários, pagamento, entrega e status
continuam vindo das regras da loja.

O atendimento é especializado em delivery: sugestões de sabores e bebidas,
cardápio, funcionamento, formas de pagamento, orientações para informar o endereço
e consulta do próprio pedido. Agora o carrinho também pode ser montado na conversa
e registrado no PDV após revisão e confirmação explícita do cliente. A IA interpreta
itens e quantidades, mas a confirmação é processada pelas regras locais. Pagamentos
nunca são confirmados pela IA. O cardápio continua disponível. Estoque, ingredientes, alergias,
prazos exatos, alterações e reclamações são encaminhados para a equipe.

Com a IA ativada, saudações e agradecimentos recebem respostas naturais locais,
sem gastar cota do Gemini. Dúvidas livres passam pela IA, e as sugestões chegam
ao WhatsApp pela fila de envio da Evolution. Assuntos não reconhecidos recebem
uma pergunta para orientar o cliente, em vez de repetir a lista de números.
O comando MENU continua disponível para retomar o robô após atendimento humano.
Uma falha ou limite da IA mantém o atendimento local disponível; isso não prova
que uma resposta foi gerada pelo Gemini. Consulte o resultado da última consulta.

## Ativar com a cota gratuita

1. Abra [Google AI Studio](https://aistudio.google.com/api-keys) e crie uma chave
   em um projeto com plano gratuito. Confira os limites exibidos para o modelo;
   não vincule faturamento nem ative o plano pago para este uso.
2. No Render, abra **sahara-esfihas-pdv → Environment** e adicione:

   | Key | Value |
   | --- | --- |
   | `SAHARA_AI_API_KEY` | A chave privada do Gemini |
   | `SAHARA_AI_ENABLED` | `1` |
   | `SAHARA_AI_DAILY_LIMIT` | `50`, ou um limite menor |
   | `SAHARA_AI_CUSTOMER_DAILY_LIMIT` | Opcional: `20` consultas por conversa/dia, limitado pela cota diária da loja |

3. Salve e faça o deploy do **PDV**. A Evolution usa a conexão já existente.
4. Abra **Integrações → WhatsApp da Sahara → Atualizar dados**. Configuração
   presente significa que as variáveis foram recebidas; não confirma validade da
   chave. Envie de outro número uma pergunta como “Que opções salgadas você sugere?”.
   Confira a resposta e **Última consulta de IA: concluída** no painel.

O código não configura cobrança nem faz upgrade de conta. A gratuidade depende
do projeto e das regras do Google; confira [preços](https://ai.google.dev/gemini-api/docs/pricing)
e [limites](https://ai.google.dev/gemini-api/docs/rate-limits) antes de ativar.
O modelo inicial é `gemini-2.5-flash-lite`. Se ele estiver indisponível, abra
**Integrações → Inteligência artificial no WhatsApp → Atualizar modelos**.
O PDV consulta a lista do Google com a chave privada e exibe somente modelos de
texto Flash-Lite que informam suporte a `generateContent`. Selecione outro modelo
listado e toque em **Usar modelo selecionado**, depois em **Testar resposta da IA**.
A seleção fica salva na base privada e passa a valer para as perguntas seguintes
do WhatsApp, os metadados e o teste de geração. Não exige editar chaves no navegador
nem reiniciar o PDV. Consultar e selecionar modelos não gera conteúdo nem consome
a cota local de geração. A lista não comprova cota gratuita: mantenha o projeto
gratuito no AI Studio e confirme a geração. Há no máximo três páginas de listagem,
com resposta e duração limitadas; uma lista incompleta não altera a seleção.
Não há troca automática por outro provedor ou modelo após uma falha de geração.
Se estiver indisponível, o atendimento básico continua e a consulta não é repetida.
Não é preciso contratar outro servidor para a IA.

## Limites e atendimento humano

O limite local inicial é 50 consultas por dia no horário de Maringá, com teto
configurável de 200. Cada número pode usar inicialmente até 20 consultas por dia,
configuráveis por `SAHARA_AI_CUSTOMER_DAILY_LIMIT` sem ultrapassar o limite da loja.
As duas cotas locais renovam à meia-noite no horário de Maringá. Perguntas seguidas
continuam passando pela IA, sem cair no atendimento básico por um intervalo de
30 segundos entre mensagens. Tentativas que falham também
contam. As cotas gratuitas do Google podem ser inferiores ou mudar.

`MENU`, `PARAR`, `ATIVAR AVISOS`, `ATENDENTE`, horários, formas de pagamento e
consultas explícitas de status continuam usando regras locais, sem consulta à IA.
O pedido pertence sempre ao número verificado no webhook. A IA não pode escolher
outro cliente, executar ferramentas, confirmar pedidos ou modificar preços. Somente
o fluxo local de confirmação pode registrar o pedido revisado no PDV.

Pedidos de ajuda humana pausam o robô por 24 horas. Consultas de IA pendentes
ou respostas geradas que ainda não foram enviadas são descartadas durante essa
pausa. A equipe continua atendendo pelo PDV. `MENU` retoma o robô.

As consultas são gravadas em uma fila privada; o webhook não aguarda o Gemini nem
mantém uma transação SQLite aberta durante uma chamada externa. Avisos e respostas
já prontos têm prioridade. Reiniciar durante uma consulta não repete a chamada:
o atendimento usa a resposta básica. Eventos duplicados não geram novas consultas.

## Dados e credenciais

A chave fica somente nas variáveis privadas do PDV. O navegador recebe apenas
estado da configuração, contagem e resultado genérico; nenhum erro bruto do
provedor ou segredo é mostrado. HTTPS e a verificação de certificado são mantidos;
redirecionamentos não recebem a chave.

O Gemini recebe o catálogo e até 800 caracteres da mensagem atual, com filtro de
telefones, e-mails, URLs e trechos de endereço. Não recebe o número do WhatsApp,
cadastro do cliente, registros de pedidos ou pagamentos. Recebe apenas o histórico
recente filtrado da própria conversa, conforme descrito abaixo. O filtro não
garante remover todos os dados pessoais escritos em linguagem livre. Consulte os
[termos do Gemini](https://ai.google.dev/gemini-api/terms), inclusive o tratamento
de conteúdo no plano gratuito, antes de ativar o atendimento da loja.

Para pausar somente a IA, configure `SAHARA_AI_ENABLED=0` e reinicie o PDV.
O menu e as funções básicas do WhatsApp continuam disponíveis.

Consultas pendentes e respostas geradas há mais de 24 horas são descartadas,
evitando responder uma conversa antiga depois de uma reconexão tardia.

## Validação

```sh
.venv/bin/python -m unittest discover -s server/tests -v
PYTHON=.venv/bin/python node tests/admin.spec.cjs
node tests/evolution-admin.spec.cjs
```

Os testes usam respostas simuladas do Gemini e uma base privada temporária.
Não usam uma chave real, não consomem cota e não enviam mensagens no WhatsApp.
A verificação real exige ativação pelo proprietário e uma mensagem de teste.

## Se receber apenas o menu básico

Em **Integrações → Inteligência artificial no WhatsApp**, o botão **Testar resposta
da IA** consulta o Gemini com uma pergunta fixa sobre sugestões e preços, mostrando
a resposta montada pelo PDV ou o motivo da falha. Não envia WhatsApp, não usa dados
de clientes e não cria pedidos ou conversas. Consome uma consulta da cota local e
da cota do provedor, com até 3 testes por dia e 30 segundos entre testes. A cota
local diária é compartilhada com o atendimento dos clientes. Uma chave de
idempotência evita repetir geração ao consultar novamente um teste interrompido.
Testes interrompidos por reinício são registrados sem nova chamada automática.
Um teste aprovado valida a geração e a interpretação da pergunta, mas o envio
real ao WhatsApp deve ser confirmado no histórico e pelo destinatário.

No PDV, abra **Integrações → Inteligência artificial no WhatsApp → Verificar IA**.
O botão fica no início da tela, inclusive quando a IA ainda não está configurada
ou a conexão com a Evolution não pode ser consultada. O servidor
consulta apenas os metadados do modelo, sem gerar conteúdo, enviar WhatsApp ou
registrar uso na cota local de geração. A verificação exige sessão administrativa
e proteção CSRF. Ela informa se a chave foi recusada, não possui permissões,
se a API está desativada, o modelo não está disponível, a cota foi recusada ou a
rede falhou. Uma verificação aprovada confirma acesso à chave/modelo; a resposta
real ainda precisa ser confirmada por uma pergunta do cliente.

Resultados são invalidados quando a chave ou a ativação muda. O resultado da
última consulta também mostra o motivo específico do atendimento básico, sem
exibir a resposta bruta do Google nem valores secretos. HTTP 400 com
`API_KEY_INVALID` identifica chave inválida; HTTP 429 identifica limite de cota.
Não ative cobrança para resolver uma cota gratuita indisponível.

Referências: [SDK oficial](https://github.com/googleapis/python-genai),
[respostas estruturadas](https://ai.google.dev/gemini-api/docs/structured-output).

O modelo recebe até seis mensagens recentes da mesma conversa, dentro das últimas
24 horas, incluindo respostas já aceitas pelo WhatsApp. Contatos, números longos
e endereços são removidos desse contexto. Não recebe conversas de outros clientes.
Respostas gerais podem conter imprecisões; informações atuais sem fonte devem ser
tratadas como incertas. Não há navegação na internet nem execução de ações pela IA.

## Pedidos completos pelo WhatsApp

1. O cliente envia **PEDIR** ou **NOVO PEDIDO**. Um carrinho privado é aberto.
2. Envia, por exemplo, **2 carne e 1 queijo**. Nomes exatos e quantidades numéricas
   são processados localmente, mesmo sem cota de IA. Solicitações em linguagem livre,
   como "quero duas de carne", podem ser interpretadas pelo Gemini; quantidades ou
   sabores ambíguos devem ser esclarecidos, nunca adivinhados.
3. **AJUSTAR 3 carne** substitui a quantidade; **REMOVER queijo** retira o produto.
   **CARRINHO** mostra o resumo e a próxima etapa; **LIMPAR CARRINHO** limpa os itens;
   **CANCELAR CARRINHO** descarta apenas o rascunho, sem cancelar pedidos registrados.
4. **FINALIZAR** inicia a coleta de nome, rua, número, bairro em Maringá, complemento,
   observações e pagamento. **SEM COMPLEMENTO** e **SEM OBSERVAÇÃO** pulam os opcionais.
   Formas: **PIX**, **DINHEIRO**, **CRÉDITO**, **DÉBITO** ou **A COMBINAR**.
5. O cliente revisa os itens, valor dos produtos, endereço e pagamento. Pode usar
   **ALTERAR CARRINHO**, **ALTERAR ENDEREÇO**, **ALTERAR PAGAMENTO**, **ALTERAR NOME** ou
   **ALTERAR OBSERVAÇÃO**. Alterar itens exige **FINALIZAR** novamente.
6. Somente **CONFIRMAR PEDIDO**, como mensagem completa na etapa de revisão, registra
   o pedido. "Sim" ou uma resposta da IA não confirmam. O PDV mostra a origem
   **Recebido pelo WhatsApp** e usa o mesmo serviço transacional do cardápio: preços
   reais, estoque, eventos e status inicial em preparo, com pagamento **não pago**.

A equipe confirma disponibilidade, atendimento do endereço, eventual taxa e prazo.
O valor apresentado é o dos produtos; o fluxo não calcula uma taxa de entrega nem
faz cobrança online. Agendamento e cupons continuam pelo cardápio/equipe. O pedido
segue as etapas normais do PDV e pode ser consultado com **STATUS**. Avisos de etapa
não são autorizados implicitamente: o cliente pode enviar **ATIVAR AVISOS**.

Carrinhos ficam na base privada, vinculados ao número autenticado no webhook e ao
provedor; sobrevivem a reinícios e expiram após 24 horas sem alterações. A chave
idempotente por carrinho evita pedidos e consumo de estoque duplicados. Pedido
registrado não é editado pelo robô: alterações seguem para **ATENDENTE**; outro
pedido começa com **NOVO PEDIDO**. A pausa humana bloqueia confirmação e respostas
pendentes do carrinho; **MENU** retoma o robô, preservando o rascunho.

Os dados de nome, endereço e pagamento coletados neste fluxo ficam no PDV. Essas
mensagens e resumos são excluídos do histórico enviado ao Gemini. O modelo recebe
somente IDs/quantidades e a etapa do carrinho, além do contexto filtrado das dúvidas.
Uma resposta de IA atrasada não pode sobrepor uma alteração posterior do carrinho.
Se os preços mudarem após o resumo, o cliente recebe os novos valores e precisa
confirmar novamente. Se o estoque não permitir o registro, todos os efeitos da
 tentativa são revertidos e o carrinho permanece disponível para revisão.

Não há variável ou credencial adicional para ativar esse checkout. Usa a conexão
existente do WhatsApp e o banco persistente do PDV; preserve ambos no Render.
