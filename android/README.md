# APK Sahara para clientes

O APK abre o cardápio atual em uma WebView Android, no endereço HTTPS `https://saharaesfihas-card.github.io/saharaesfihas/?app=1`. Os preços, produtos e melhorias do site aparecem no app quando ele acessa a versão online. A primeira abertura precisa de internet; a disponibilidade do cache offline depende da versão do Android System WebView e não substitui a conexão necessária para enviar pedidos.

## Instalação e funcionamento

[Baixar Sahara 2026.10.04](https://saharaesfihas-card.github.io/saharaesfihas/downloads/sahara-esfihas-2026.10.04.apk).

No Android, abra o arquivo baixado e, se o sistema solicitar, autorize a instalação somente para o navegador ou gerenciador usado no download. Não é necessário instalar também a versão PWA. Mantenha o Android System WebView atualizado.

O cliente escolhe os produtos, informa o endereço e continua no WhatsApp. O seletor **Abrir com** permite escolher o WhatsApp ou um navegador; a mensagem ainda precisa ser revisada e enviada pelo cliente. Mapas e outros links HTTPS externos também saem pelo seletor do Android. A loja confirma disponibilidade, entrega e pagamento.

A localização é solicitada ao tocar em **Usar minha localização**. O cliente pode negar o acesso, conceder localização aproximada ou precisa e continuar usando os campos de endereço. Depois de conferir o ponto no mapa, precisa tocar em **Usar este local**. Não há rastreamento contínuo, e o app não solicita localização na abertura. O acesso é limitado à origem HTTPS do cardápio.

O APK é o app do cliente. Ele não hospeda o servidor do PDV, não ativa chatbot com IA e não realiza pagamentos online. O painel administrativo e as integrações continuam dependendo do servidor e das contas oficiais configuradas.

## Identificação e atualização

- Pacote: `br.com.saharaesfihas.app`.
- Versão: `2026.10.04`; código de versão: `20261004`.
- Android mínimo: 6.0 / API 23; versão alvo: Android 15 / API 35.
- Certificado de assinatura, SHA-256 público: `837738fa49eef6c727d92abd1f542b2390563c25169a096a0f3a342ccd88d2c6`.
- APK desta versão, SHA-256: `7483bd1cf5da198d9d8f43ae31a5cddb155a7a1bf03b9d938d50f04cab0dd532`.

Não havia APK nem certificado anterior disponível para comparação. Por isso, não está garantido que este arquivo atualize uma instalação antiga por cima. Para próximas versões, mantenha o mesmo pacote e a mesma assinatura e aumente o código de versão em `build_apk.py`. Preserve a assinatura desta versão para os próximos APKs.

## Compilação

Requisitos: Linux x86-64, Python 3, Java 21 com `java` e `keytool` no PATH e acesso aos endereços de download registrados em `toolchain.lock.json`. A compilação usa o Eclipse ECJ, sem Gradle, AndroidX ou dependência de `javac`.

Na raiz do repositório:

```sh
python3 android/prepare_tools.py
python3 android/build_apk.py
```

`prepare_tools.py` baixa o Android SDK Platform 35 e Build Tools 35.0.1 da distribuição oficial do Google e o Eclipse ECJ 3.42.0 publicado no Maven Central. Todos os arquivos têm SHA-256 fixado em `toolchain.lock.json`; um checksum diferente interrompe a preparação. As ferramentas ficam em `/workspace/.sahara-android-tools`, configurável pela variável `SAHARA_ANDROID_TOOLS`.

`build_apk.py` compila recursos, código Java e DEX, alinha o pacote e gera `downloads/sahara-esfihas-2026.10.04.apk` e seu arquivo `.sha256`. Depois verifica as assinaturas v1/v2/v3, alinhamento, manifesto e integridade do ZIP.

## Preservar a assinatura

Na primeira compilação, o script cria uma chave privada PKCS12 em `/workspace/.sahara-android-signing/sahara-release.p12` e uma senha aleatória em `/workspace/.sahara-android-signing/store-password`. O diretório tem permissão `0700`, e os dois arquivos têm `0600`. Essa pasta fica fora do repositório; outro local externo pode ser configurado por `SAHARA_ANDROID_SIGNING`.

Faça um backup privado dos dois arquivos antes de descartar este ambiente. Restaure os dois juntos no mesmo diretório de assinatura para futuras compilações. Não publique a chave, não a inclua no Git nem envie a chave ou a senha pelo chat. Somente o certificado público e o APK assinado podem ser distribuídos. Se apenas um dos dois arquivos estiver presente, o build interrompe a execução para evitar criar uma assinatura diferente.

## Verificação e limites

O APK foi compilado e passou nas verificações de assinatura v1/v2/v3, manifesto, alinhamento e integridade. Não havia emulador nem aparelho Android disponível: o GPS, o seletor de aplicativos, a abertura do WhatsApp e o cache precisam ser conferidos em um celular antes de afirmar que foram testados no Android.

No aparelho, confira a abertura do cardápio, o teclado e as barras do sistema; teste localização permitida e negada; revise um pedido no WhatsApp sem enviá-lo; abra o mapa; e teste falta de conexão e **Tentar novamente**. Essas verificações não devem criar pedidos nem enviar mensagens reais.
