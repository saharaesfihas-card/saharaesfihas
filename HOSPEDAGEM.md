# Hospedar o PDV Sahara

O projeto está preparado para hospedar o cardápio, o painel de gestão e a API juntos, com HTTPS e banco persistente. **Esta preparação não cria uma hospedagem:** é necessário aplicar a configuração na conta do responsável pela loja. O ambiente de trabalho do Codex não é um servidor permanente para a operação.

## Opção Hostinger VPS

A preparação para um VPS com Docker Compose e Caddy está em [HOSTINGER.md](HOSTINGER.md). Ela inclui `compose.hostinger.yaml`, `Caddyfile.hostinger` e `.env.hostinger.example`, com HTTPS, aplicação interna e volumes persistentes. A ativação depende de acesso ao VPS e de um domínio ou subdomínio apontando ao servidor. Os recursos disponíveis em outro plano da Hostinger precisam ser confirmados no plano contratado.

O Render permanece como alternativa abaixo. Nenhum dos caminhos cria hospedagem somente pela presença desses arquivos no repositório.

## Ativação pelo Render

Use o botão abaixo depois que os arquivos de hospedagem estiverem publicados na branch `main` do repositório:

[Configurar a hospedagem no Render](https://render.com/deploy?repo=https://github.com/saharaesfihas-card/saharaesfihas)

1. Entre na sua conta do Render ou crie uma conta. Autorize o acesso ao repositório `saharaesfihas-card/saharaesfihas` quando solicitado.
2. Confira o serviço `sahara-esfihas-pdv`. O arquivo `render.yaml` propõe o plano **Starter**, uma instância e um disco persistente de **1 GB**. O serviço e o disco são pagos; confira os valores e a cobrança apresentados pelo Render antes de aplicar.
3. No campo seguro `SAHARA_ADMIN_PASSWORD` solicitado pela configuração, informe uma senha exclusiva com pelo menos oito caracteres. Essa será a senha de entrada no painel. Não envie a senha pelo chat nem a coloque no repositório.
4. Aplique a configuração pelo botão mostrado pelo Render e aguarde a conclusão do build e da implantação. A publicação somente estará concluída quando o serviço estiver ativo e as verificações abaixo passarem.

O Render entrega o endereço HTTPS do serviço. O cardápio ficará na raiz desse endereço e o painel em `/admin.html`. Use a senha configurada para entrar no painel e abrir **Novo pedido · PDV**.

## Confirmar que está funcionando

No endereço HTTPS fornecido pelo Render:

1. Abra `/api/health`. A resposta deve ter `ready: true` e `admin_configured: true`.
2. Abra `/admin.html` e entre com sua senha. Confirme que o catálogo aparece no PDV e que o painel carrega sem erro de sessão.
3. Abra a raiz do endereço para conferir o cardápio. O servidor entrega `system-config.js` com a API `/api`, ativando o registro de solicitações no painel.

A verificação de saúde indica que o processo responde; o login confirma que a senha e a sessão funcionam. A hospedagem não ativa automaticamente WhatsApp Business, pagamentos online, iFood ou emissão fiscal. As pendências dessas integrações continuam visíveis no painel.

## Endereço atual do site e aplicativo

O [site atual no GitHub Pages](https://saharaesfihas-card.github.io/saharaesfihas/) continua disponível. Ele usa o WhatsApp enquanto o seu `system-config.js` não tiver o endereço da API hospedada.

Depois de confirmar o novo endereço HTTPS e o funcionamento do painel, há duas opções:

- Usar o cardápio servido pelo novo endereço, que já se conecta à própria API.
- Manter o endereço do GitHub Pages e atualizar seu `system-config.js` para `apiBase: 'https://ENDERECO-DO-SERVICO/api'`, substituindo o exemplo pelo endereço real. A origem do GitHub Pages já está permitida no `render.yaml`. Esse arquivo deve conter apenas o endereço público, sem senhas ou tokens.

O painel administrativo deve ser aberto no domínio do servidor, em `/admin.html`. O aplicativo instalado no endereço antigo continua ligado àquele endereço; para usar o novo endereço, abra o novo cardápio e instale o aplicativo por ele.

## Configuração entregue

| Item | Configuração |
| --- | --- |
| Aplicação | Docker com Python 3.12, Node.js e dados de fuso horário. |
| Acesso | Cardápio, painel e API na raiz do mesmo domínio HTTPS. |
| Persistência | Disco em `/var/lib/sahara`; banco privado em `/var/lib/sahara/data`. |
| Execução | Um worker e uma instância, adequados ao banco SQLite local. |
| Credencial | `SAHARA_ADMIN_PASSWORD` informada na configuração segura do provedor. |
| Sessão | Cookie seguro para HTTPS; painel e API na mesma origem. |
| Atualizações | `autoDeployTrigger: off`: novas alterações no GitHub não implantam automaticamente o servidor. |

O inicializador gera o hash da senha em memória e remove a senha original do ambiente do processo antes de iniciar a aplicação. A credencial continua configurada na conta do provedor para permitir os próximos reinícios. O processo ajusta as permissões apenas do diretório privado e dos arquivos conhecidos do SQLite e, em seguida, executa a aplicação com o usuário sem privilégios `10001`.

Se preferir configurar somente um hash, gere-o com `python -m server.set_password` e use a variável segura `SAHARA_ADMIN_PASSWORD_HASH` em vez de `SAHARA_ADMIN_PASSWORD`. Não configure as duas opções ao mesmo tempo. O hash também é uma credencial e não deve ser enviado pelo chat ou publicado.

## Atualizações e dados

Depois de publicar uma alteração na branch `main`, faça uma implantação manual pelo painel do Render. Os produtos e preços do cardápio são carregados ao iniciar o servidor; os pedidos anteriores preservam os valores registrados. O disco deve ser mantido ao atualizar ou reiniciar o serviço. Excluir o disco remove os dados que estão nele.

Faça cópias de segurança frequentes pelo mecanismo de backup do SQLite e teste a restauração. Com o servidor em funcionamento, copiar apenas `sahara.sqlite3` pode perder alterações presentes no arquivo WAL. Também é possível parar o serviço antes de copiar o diretório privado inteiro. Mantenha as cópias fora de locais públicos. Disco persistente e cópia de segurança atendem necessidades diferentes.

Não aumente o número de instâncias nem compartilhe o banco por armazenamento de rede sem rever a arquitetura. Para esta implantação, o SQLite e os seus arquivos auxiliares precisam permanecer no mesmo disco local persistente.

## Outro provedor ou servidor próprio

O `Dockerfile` também permite usar um provedor de containers ou servidor próprio que ofereça disco persistente e HTTPS. A imagem pode ser criada com `docker build -t sahara-pdv .`. Defina uma das credenciais administrativas, `SAHARA_DATA_DIR` fora de `/app`, e a porta numérica `PORT` esperada pelo provedor. A porta padrão é `10000`.

Configure o proxy para preservar o `Host` público e enviar `X-Forwarded-Proto: https`. Ajuste `FORWARDED_ALLOW_IPS` para confiar somente no proxy autorizado. O valor `*` do Blueprint é específico do serviço gerenciado, onde o acesso público passa pelo proxy do Render; não o reutilize quando o processo puder receber conexões diretas de clientes.

Publique a aplicação na raiz do domínio. Caminhos como `/loja/` exigem alterações nos caminhos da API, no cookie e no aplicativo. Mantenha `SAHARA_COOKIE_SECURE=1` em produção. Sem HTTPS, o navegador não envia o cookie seguro e o login não permanece ativo.

Os detalhes de operação, integrações e testes estão em [SISTEMA.md](SISTEMA.md).
