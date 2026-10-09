# IA para delivery no WhatsApp da Sahara

O robô conectado por QR Code pode usar o **Gemini 2.5 Flash-Lite** para
interpretar mensagens em linguagem natural e selecionar sugestões do cardápio.
O modelo retorna somente a intenção e IDs de produtos. O PDV monta o texto com
nomes e preços cadastrados; não publica texto livre gerado pelo modelo.

O atendimento é especializado em delivery: sugestões de sabores e bebidas,
cardápio, funcionamento, formas de pagamento, orientações para informar o endereço
e consulta do próprio pedido. Não cria pedidos nem confirma pagamentos pelo chat.
As solicitações são finalizadas no cardápio. Estoque, ingredientes, alergias,
prazos exatos, alterações e reclamações são encaminhados para a equipe.

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

3. Salve e faça o deploy do **PDV**. A Evolution usa a conexão já existente.
4. Abra **Integrações → WhatsApp da Sahara → Atualizar dados**. Configuração
   presente significa que as variáveis foram recebidas; não confirma validade da
   chave. Envie de outro número uma pergunta como “Que opções salgadas você sugere?”.
   Confira a resposta e **Última consulta de IA: concluída** no painel.

O código não configura cobrança nem faz upgrade de conta. A gratuidade depende
do projeto e das regras do Google; confira [preços](https://ai.google.dev/gemini-api/docs/pricing)
e [limites](https://ai.google.dev/gemini-api/docs/rate-limits) antes de ativar.
O modelo é fixado no servidor, sem troca automática por outro provedor ou modelo.
Se estiver indisponível, o atendimento básico continua e a consulta não é repetida.
Não é preciso contratar outro servidor para a IA.

## Limites e atendimento humano

O limite local inicial é 50 consultas por dia no horário de Maringá, com teto
configurável de 200. Cada número pode usar até 10 consultas em 24 horas, com
intervalo mínimo de 30 segundos entre chamadas de IA. Tentativas que falham também
contam. As cotas gratuitas do Google podem ser inferiores ou mudar.

`MENU`, `PARAR`, `ATIVAR AVISOS`, `ATENDENTE`, horários, formas de pagamento e
consultas explícitas de status continuam usando regras locais, sem consulta à IA.
O pedido pertence sempre ao número verificado no webhook. A IA não pode escolher
outro cliente, executar ferramentas, criar pedidos ou modificar preços.

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
cadastro do cliente, histórico de conversa, pedidos ou pagamentos. O filtro não
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
