# Sahara no celular

App para clientes, publicado em https://saharaesfihas-card.github.io/saharaesfihas/?app=1.

## Instalação pelo navegador

- Android: abra no Chrome, toque em **Instalar app** e confirme. Se necessário, use o menu ⋮ → **Instalar app** ou **Adicionar à tela inicial**.
- iPhone: abra no Safari e use **Compartilhar** → **Adicionar à Tela de Início**.
- Computador: abra no Chrome ou Edge e use o botão de instalação da barra de endereço.

O Sahara abre pela tela inicial em uma janela própria quando instalado como PWA. Essa opção continua disponível sem baixar um APK.

## APK Android

Também há um [APK Sahara 2026.10.04](https://saharaesfihas-card.github.io/saharaesfihas/downloads/sahara-esfihas-2026.10.04.apk). Ele atende Android 6.0 ou superior e abre o site atual em uma WebView. Não é preciso instalar as duas versões. No APK, a primeira abertura exige internet e o cache depende do Android System WebView instalado; pedidos pelo WhatsApp precisam de conexão.

Ao abrir mapas ou continuar no WhatsApp, o APK apresenta o seletor do Android. O cliente escolhe o aplicativo, revisa a mensagem e a envia. A permissão de localização aparece somente após tocar em **Usar minha localização**, e o endereço manual continua disponível se ela for negada.

O servidor do PDV está no Render, e o site aberto pelo APK registra as solicitações na API `https://sahara-esfihas-pdv.onrender.com/api`. Essa conexão não exige reinstalar o APK. O [painel da loja](https://sahara-esfihas-pdv.onrender.com/admin.html) usa a senha configurada no Render. IA e pagamentos online continuam dependendo das integrações oficiais.

Não havia assinatura anterior para comparar, portanto não é garantida a atualização sobre um APK antigo. A versão atual foi compilada e teve assinatura e manifesto verificados; GPS e WhatsApp ainda precisam de teste em aparelho Android.

Consulte [`android/README.md`](android/README.md) para identificação do pacote, checksums, compilação e preservação privada da chave de assinatura.

## Pedido

Escolha as quantidades, revise a sacola, preencha o endereço e selecione a forma de pagamento. **Continuar no WhatsApp** registra a solicitação no PDV e prepara uma mensagem; o cliente revisa e envia. A loja confirma disponibilidade, entrega e pagamento. Não há cobrança automática neste app. Se o registro falhar, a sacola e o endereço são preservados para tentar novamente.

Para facilitar a entrega, toque em **Usar minha localização** quando estiver no endereço do pedido. Permita o acesso no navegador, confira o ponto no mapa e toque em **Usar este local**. Informe o número da casa e, se necessário, um complemento. A rua e o bairro tornam-se opcionais, e a mensagem do WhatsApp inclui um link do ponto confirmado e a precisão aproximada. A localização não preenche automaticamente o nome da rua nem o bairro.

O GPS só é solicitado ao tocar no botão, em HTTPS ou no servidor local. Não há rastreamento contínuo nem consulta a serviços de geocodificação. O ponto não é gravado permanentemente no dispositivo; a atualização solicitada do app preserva os dados apenas na aba atual. **Remover localização** volta ao preenchimento manual. Se a permissão for negada ou o GPS falhar, os campos de endereço continuam disponíveis.

A sacola é salva no navegador/dispositivo. Após a primeira visita online e a preparação do cache, o cardápio e as fotos podem ser consultados sem internet. O aviso indica que os dados salvos podem estar desatualizados; é necessária conexão para continuar no WhatsApp.

## Atualizações

O service worker busca os arquivos da rede primeiro para exibir preços atuais e usa o cache quando a conexão falha. Uma nova versão do worker apresenta **Atualizar app**; a sacola permanece salva e o formulário da aba é recuperado após essa atualização. No APK, isso depende do suporte do Android System WebView; mudanças no código Android exigem instalar um novo APK assinado com a mesma chave.

Arquivos: `manifest.webmanifest`, `service-worker.js`, `pwa.js`, `pwa.css` e `icons/`. O manifesto e os caminhos usam o escopo `/saharaesfihas/` do GitHub Pages. Ao mudar a lista de arquivos do cache, altere também a versão `CACHE` no worker. O `app.js` continua contendo o cardápio, as fotos e os preços usados pelo Sahara Admin.

## Testes da localização

No ambiente de desenvolvimento com Node.js, Python 3, Playwright e Chromium, execute `node tests/address.spec.cjs`. O teste inicia e encerra seu próprio servidor local, simula posições e falhas do GPS e intercepta a abertura do WhatsApp. Não acessa serviços externos nem envia pedidos. Para outro executável de Chromium, defina `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`.
