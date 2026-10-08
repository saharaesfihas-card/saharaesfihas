# Revisão do layout do PDV

O pedido é um redesenho do painel existente inspirado na Saipos e nas imagens da Brendi fornecidas pelo usuário, mantendo a identidade e os recursos da Sahara. A operação continua somente delivery. O fluxo solicitado tem quatro etapas: preparo na cozinha, pedido pronto, a caminho do endereço e entregue.

## Evidência de navegador

Capturas da execução atual de Chromium, com banco temporário e cliente fictício, sem pedidos em produção:

- `/tmp/sahara-four-stage-shots/login-desktop.png` — login, 1280 × 900.
- `/tmp/sahara-four-stage-shots/orders-desktop.png` — pedido em preparo, 1280 × 900.
- `/tmp/sahara-four-stage-shots/pdv-desktop.png` — catálogo, cliente e subtotal, 1280 × 900.
- `/tmp/sahara-four-stage-shots/drawer-mobile.png` — menu lateral aberto, 390 × 844.
- `/tmp/sahara-four-stage-shots/orders-mobile.png` — histórico e quatro etapas, 390 × 844.
- `/tmp/sahara-four-stage-shots/pdv-mobile.png` — seleção de produtos, 390 × 844.

Densidade das capturas: 1 pixel por pixel CSS. As capturas rejeitadas durante a animação do menu foram substituídas após esperar as transições terminarem. As imagens em `/tmp` são evidência desta execução, não arquivos permanentes da aplicação.

## Qualidade do painel implementado

- Tipografia: fonte sans do sistema, títulos e valores com hierarquia consistente, identificadores curtos nos cartões e identificadores completos preservados nas operações e comandas.
- Espaçamento: menu fixo no computador e gaveta no celular, painéis com bordas discretas, cartões de pedidos mais largos e catálogo separado do resumo.
- Cores: vinho da Sahara nos controles principais, rosa no item ativo e cores distintas para as etapas, sempre acompanhadas de texto.
- Imagens: marca original da Sahara e ícones locais Bootstrap Icons 1.13.1 com licença MIT. Sem dependência de CDN ou marcas de terceiros no produto.
- Conteúdo: métricas vêm do servidor, nenhum registro demonstrativo na aplicação e integrações externas continuam indicando sua disponibilidade real.

A revisão corrigiu cartões estreitos que quebravam os títulos e o resumo que saía da área visível durante o preenchimento. A captura final do PDV e a verificação de posição confirmam que subtotal e botão de registro ficam visíveis em 1280 × 900. Em telas menores, os campos seguem o fluxo da página.

## Interações verificadas

24 verificações do painel passaram, com backend e banco isolados: login e sessão, busca de produtos e pedidos, categoria sem perda do rascunho, cálculo do subtotal, validação de campos em seções recolhidas, registro e quatro etapas, estoque, cupom, caixa, fiado, fidelidade, impressão, menu com Escape e foco por teclado, e todos os módulos sem transbordamento horizontal em 390 e 800 pixels. Não houve erros de JavaScript.

Também passaram 48 testes da API, 12 verificações do checkout e 5 do aplicativo/PWA. A API bloqueia o salto direto do preparo para a entrega e não duplica estoque, uso do cupom ou recebimentos em tentativas repetidas.

## Limite da comparação com a referência

As duas imagens originais foram inspecionadas antes da edição no chat. Seus caminhos temporários (`1-1001567365.jpg` e `2-1001567364.jpg`) ficaram indisponíveis na retomada do ambiente. Assim, não foi possível repetir uma comparação combinada entre a referência original e a captura final. A validação funcional e a revisão visual do painel implementado passaram; a comparação formal de fidelidade visual está bloqueada pela indisponibilidade desses arquivos.

final result: blocked
