# Sahara no celular

App para clientes, publicado em https://saharaesfihas-card.github.io/saharaesfihas/?app=1.

## Instalação

- Android: abra no Chrome, toque em **Instalar app** e confirme. Se necessário, use o menu ⋮ → **Instalar app** ou **Adicionar à tela inicial**.
- iPhone: abra no Safari e use **Compartilhar** → **Adicionar à Tela de Início**.
- Computador: abra no Chrome ou Edge e use o botão de instalação da barra de endereço.

Não é preciso baixar um APK. O Sahara abre pela tela inicial em uma janela própria quando instalado como app.

## Pedido

Escolha as quantidades, revise a sacola, preencha o endereço e selecione a forma de pagamento. **Continuar no WhatsApp** prepara uma mensagem; o cliente revisa e envia. A loja confirma disponibilidade, entrega e pagamento. Não há cobrança automática neste app.

A sacola é salva no navegador/dispositivo. Após a primeira visita online e a preparação do cache, o cardápio e as fotos podem ser consultados sem internet. O aviso indica que os dados salvos podem estar desatualizados; é necessária conexão para continuar no WhatsApp.

## Atualizações

O service worker busca os arquivos da rede primeiro para exibir preços atuais e usa o cache quando a conexão falha. Uma nova versão do worker apresenta **Atualizar app**; a sacola permanece salva e o formulário da aba é recuperado após essa atualização.

Arquivos: `manifest.webmanifest`, `service-worker.js`, `pwa.js`, `pwa.css` e `icons/`. O manifesto e os caminhos usam o escopo `/saharaesfihas/` do GitHub Pages. Ao mudar a lista de arquivos do cache, altere também a versão `CACHE` no worker. O `app.js` continua contendo o cardápio, as fotos e os preços usados pelo Sahara Admin.
