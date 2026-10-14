# Pausar e reativar produtos pelo PDV

## Como usar

1. Entre na gestão em `https://sahara-esfihas-pdv.onrender.com/admin.html`.
2. No menu **Operação**, abra **Cardápio**.
3. Busque o produto ou filtre por categoria e disponibilidade.
4. Toque em **Pausar item** quando o produto estiver indisponível. Ele permanece
   cadastrado na gestão, com situação **Pausado**, e fica oculto no cardápio dos clientes.
5. Quando o produto voltar, toque em **Ativar item**. Ele volta a aparecer no cardápio.

O comando é aplicado ao banco ao salvar. Clientes com a página aberta e conexão
ativa consultam a disponibilidade automaticamente a cada **3 segundos**. Ao
retornar à aba, recuperar a conexão ou focar a janela, há uma nova consulta.
O intervalo de 3 segundos pressupõe o servidor disponível e uma resposta normal
da rede; com conexão lenta, a atualização pode demorar mais.

## O que acontece com os pedidos

- Produtos pausados não aparecem para montar novos pedidos no PDV nem no cardápio.
- Se o cliente já tiver o item no carrinho, a próxima atualização retira apenas
  esse item, recalcula os valores e avisa o cliente. Reativar não recoloca o item
  automaticamente no carrinho; o cliente escolhe novamente.
- Uma página antiga pode tentar comprar antes de receber a atualização. O servidor
  confere a disponibilidade na mesma transação do pedido e rejeita o item pausado,
  sem criar pedido, consumir estoque ou registrar pagamento.
- Pedidos que já foram registrados continuam com os itens e valores originais.
  Uma tentativa repetida do mesmo pedido já registrado não cria um novo pedido.
- O registro compartilhado de pedidos pelo WhatsApp também respeita a pausa.
  O atendimento por IA recebe somente os produtos ativos para sugestões e preços.
- O cadastro do produto e suas fichas de estoque são preservados. A pausa é manual;
  entradas de estoque não reativam produtos automaticamente.

Este controle atua no cardápio próprio do Sahara. Não publica automaticamente
alterações no catálogo da conta iFood; a integração do iFood continua separada.

## Persistência e conexão

A situação de cada item fica no SQLite privado do PDV, no disco persistente.
Reiniciar ou atualizar o servidor preserva as pausas. A migração mantém produtos
existentes ativos inicialmente; não apaga nem recria produtos ou pedidos.

O cardápio no Render e o publicado no GitHub Pages usam a mesma API do PDV.
A consulta pública mostra somente itens ativos. A listagem privada mantém ativos
e pausados para que todos possam ser reativados, inclusive se todos foram pausados.

No primeiro carregamento, o cardápio precisa consultar a disponibilidade antes
de exibir os produtos e permitir pedidos. Se não conseguir, avisa o cliente e
tenta novamente. Após um carregamento válido, uma falha de atualização informa
que a disponibilidade será conferida ao finalizar. A API e os estados de
disponibilidade não ficam no cache offline do service worker.

## Publicação

Depois de atualizar a branch `main`, no Render abra **sahara-esfihas-pdv → Manual
Deploy → Deploy latest commit**. Mantenha o disco persistente existente.
Não são necessárias credenciais novas nem um serviço adicional.
Recarregue o PDV e o cardápio após implantar para carregar os novos controles.

## Verificação

```sh
python -m unittest discover -s server/tests -q
node tests/admin.spec.cjs
node tests/ordering.spec.cjs
```

Os testes usam banco descartável e páginas locais; não pausam produtos na loja
real, não registram pedidos de produção e não enviam WhatsApp.
