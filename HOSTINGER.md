# PDV Sahara em VPS Hostinger

Esta configuração prepara o cardápio, o painel e a API para um **VPS com Docker Compose**, usando Caddy para HTTPS e um volume privado para o banco. Não cria um VPS, não contrata serviços e não instala nada na conta da Hostinger. A implantação precisa de acesso autorizado ao servidor e de um domínio ou subdomínio da loja.

Se o plano contratado for hospedagem de sites, confirme no painel ou com o provedor se ele permite containers ou processos Python permanentes com armazenamento persistente. O roteiro abaixo usa um VPS com acesso SSH e Docker; as possibilidades de outro plano dependem das suas características. A alternativa Render continua descrita em [HOSPEDAGEM.md](HOSPEDAGEM.md).

## Antes de implantar

Tenha um VPS com Docker Engine e Docker Compose v2, acesso SSH e um domínio ou subdomínio que você controla. Use as instruções oficiais do provedor para instalar o Docker, caso ele ainda não esteja disponível. Não envie senhas, chaves SSH ou tokens pelo chat ou pelo repositório.

Configure o registro DNS **A** do endereço escolhido para o IPv4 do VPS. Só mantenha um registro **AAAA** se o IPv6 também chegar ao mesmo servidor. Libere as portas **80/TCP** e **443/TCP** no firewall do VPS e do provedor; **443/UDP** é opcional para HTTP/3. Essas portas precisam estar livres para o Caddy. A porta `10000` da aplicação permanece dentro da rede Docker e não deve ser publicada no servidor.

Use o domínio na raiz, como `pdv.seudominio.com`. Não use um caminho como `seudominio.com/loja/`. O Caddy só poderá emitir o certificado público depois que o DNS e a conectividade estiverem corretos.

## Configurar e iniciar no VPS

No servidor, obtenha o repositório na branch `main` e entre na pasta do projeto. Se ele já estiver clonado, use essa cópia existente.

```bash
git clone https://github.com/saharaesfihas-card/saharaesfihas.git
cd saharaesfihas
```

Crie o arquivo privado de configuração e edite-o no próprio servidor:

```bash
umask 077
cp .env.hostinger.example .env
chmod 600 .env
nano .env
```

Preencha `SAHARA_DOMAIN` com o domínio real, sem `https://`, caminhos ou porta. Preencha `SAHARA_ADMIN_PASSWORD` com uma senha exclusiva de pelo menos oito caracteres. Use aspas simples no valor da senha para evitar a interpolação de caracteres como `$` pelo Compose. O arquivo de exemplo não oferece uma senha padrão.

O `.env` é ignorado pelo Git e pelo contexto do build. Ele permanece no servidor para permitir os próximos reinícios e deve continuar privado. O inicializador gera o hash em memória e remove a senha original do ambiente do processo antes de iniciar o serviço. O dono do VPS e do Docker ainda controla a configuração dos containers; mantenha esse acesso restrito.

Valide a configuração sem imprimir os valores privados e inicie os serviços:

```bash
docker compose --env-file .env -f compose.hostinger.yaml config --quiet
docker compose --env-file .env -f compose.hostinger.yaml up -d --build
docker compose --env-file .env -f compose.hostinger.yaml ps
```

Não execute `docker compose config` sem `--quiet` em telas ou registros compartilhados: a saída resolvida contém as variáveis configuradas. O comando de inicialização constrói a aplicação, cria os volumes e inicia os containers. O Caddy espera a aplicação ficar saudável antes de iniciar.

## Confirmar a ativação

Depois que os containers estiverem funcionando e o certificado for emitido:

1. Abra `https://SEU-DOMINIO/api/health`, substituindo o exemplo pelo domínio escolhido. A resposta deve ter `ready: true` e `admin_configured: true`.
2. Abra `https://SEU-DOMINIO/admin.html`, entre com sua senha e confira **Novo pedido · PDV** e o catálogo.
3. Abra `https://SEU-DOMINIO/` para o cardápio. Ele já usa a API da própria origem para registrar solicitações no painel.

Se o serviço não iniciar, consulte os registros localmente:

```bash
docker compose --env-file .env -f compose.hostinger.yaml logs --tail 80 app caddy
```

O log da aplicação informa senha ausente ou curta; o log do Caddy ajuda a identificar DNS, portas ou certificado. O login pode falhar quando o domínio público ou o protocolo HTTPS não chegam corretamente ao aplicativo. Esta configuração preserva o domínio e usa o Caddy como único proxy de entrada. Não ative outro caminho público direto para a aplicação.

Só considere a hospedagem concluída depois que o endereço HTTPS e o login funcionarem. A preparação não ativa integrações externas de WhatsApp, pagamentos, iFood ou emissão fiscal.

## Site e aplicativo existentes

O site atual no GitHub Pages continua disponível. Depois de verificar o novo HTTPS e o painel, você pode usar o novo cardápio ou conectar o site antigo à nova API, conforme [HOSPEDAGEM.md](HOSPEDAGEM.md). Não altere o endereço da API antes dessa verificação.

O aplicativo instalado no endereço antigo continua associado àquele endereço. O APK distribuído e o aplicativo instalável devem ser conferidos quanto ao endereço que abrem antes de mudar a operação para o novo domínio. A hospedagem do PDV e a distribuição do aplicativo são etapas separadas.

## Persistência e atualização

| Volume | Dados |
| --- | --- |
| `sahara-hostinger-pedidos` | Diretório privado do banco SQLite, incluindo seus arquivos WAL e SHM. |
| `sahara-hostinger-certificados` | Certificados e conta de emissão usados pelo Caddy. |
| `sahara-hostinger-caddy-config` | Dados de configuração persistente do Caddy. |

Os nomes dos volumes permanecem estáveis entre reinícios e atualizações. A aplicação usa um worker e um volume local, com execução sem privilégios após o ajuste inicial das permissões. Não configure `user:10001` no Compose: o inicializador precisa ajustar o diretório privado antes de reduzir seus privilégios.

Antes de atualizar, faça uma cópia de segurança consistente do SQLite e mantenha a imagem anterior. Não use `docker compose down -v`, porque essa opção remove os volumes e seus dados. Faça backups pelo mecanismo próprio do SQLite ou pare a aplicação antes de copiar o diretório privado inteiro. Copiar apenas o arquivo principal com o serviço ativo pode deixar alterações no WAL para trás. Teste a restauração e mantenha cópias privadas fora do VPS.

Para preservar a imagem atual antes de construir a próxima, escolha uma etiqueta identificável e guarde também a revisão do código correspondente. Por exemplo:

```bash
docker image tag sahara-pdv:hostinger sahara-pdv:antes-da-atualizacao
git pull --ff-only
docker compose --env-file .env -f compose.hostinger.yaml up -d --build
```

Valide novamente a saúde, o login e os pedidos após a atualização. Para voltar à imagem preservada, confirme primeiro se ela é compatível com o banco atual e use:

```bash
docker image tag sahara-pdv:antes-da-atualizacao sahara-pdv:hostinger
docker compose --env-file .env -f compose.hostinger.yaml up -d --no-build --force-recreate app
```

Esse procedimento preserva os volumes. Ele não restaura alterações feitas no banco pela versão nova; quando necessário, a restauração exige o backup e uma parada planejada do serviço.

## Rede e integrações futuras

A aplicação está numa rede Docker interna, sem acesso direto à internet. O Caddy participa também de uma rede com saída para emitir e renovar certificados. O valor `FORWARDED_ALLOW_IPS=*` só é apropriado com a aplicação isolada dessa forma e sem publicação da sua porta no host.

Integrações externas futuras podem exigir saída HTTPS autorizada da aplicação. Revise a rede e conecte os provedores quando essas integrações forem realmente implantadas. Não publique a porta do aplicativo como forma de permitir saída de rede. O guia de funcionalidades e pendências está em [SISTEMA.md](SISTEMA.md).
