# Endereço público do cardápio

O cardápio pode usar um Render Static Site gratuito, com os pedidos e preços do
servidor PDV existente. O site público não hospeda outro banco nem outro PDV.

Configuração do site estático:

- Nome: `sahara-esfihas-cardapio`.
- Repositório/branch: este repositório, `main`.
- Build: `node server/build-public-site.cjs`.
- Diretório de publicação: `.render-cardapio-public`.
- Redirecionamento: `/index.html` → `/`.
- Deploys automáticos: a cada commit na `main`.

O build copia uma lista explícita de arquivos do cliente, imagens e ícones.
Não publica administração, código do servidor, banco, credenciais ou downloads.
O arquivo público `system-config.js` aponta para a API do PDV existente.

No servidor PDV, mantenha `SAHARA_PUBLIC_URL` no endereço do próprio backend.
Defina `SAHARA_MENU_URL` para a URL real retornada pelo site estático e acrescente
essa origem à lista existente de `SAHARA_ALLOWED_ORIGINS`, sem remover as atuais.
Assim, preços e pedidos funcionam entre os sites, o WhatsApp divulga o cardápio
novo, e Origin/webhook da Evolution continuam no servidor original.

A administração continua em `/admin.html` do servidor PDV. O endereço antigo do
cardápio e `/index.html` redirecionam para o menu público configurado, mantendo
os parâmetros. As URLs de acompanhamento dos pedidos e as integrações privadas
continuam na API original.

Alterações futuras no frontend são publicadas automaticamente no Static Site.
Alterações no backend ainda precisam do deploy manual do PDV; quando os dois
mudarem juntos, verifique ambos no mesmo commit antes de concluir a publicação.

O nome solicitado depende da disponibilidade do subdomínio no Render. A URL
final deve ser conferida na resposta de criação e acessada antes de divulgar.
