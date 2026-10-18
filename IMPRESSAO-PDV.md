# Avisos e impressão de pedidos

O PDV pode tocar um aviso de novo pedido e preparar **duas vias idênticas** da
comanda, tanto na impressão automática quanto em **Imprimir comanda**.
As duas vias são páginas separadas de um único trabalho. Deixe o número de
cópias do navegador/driver em **1**, para não sair quatro vias.

## Ativar o toque

1. O som fica autorizado por padrão e sua preferência fica salva **neste
   navegador**. Ao entrar ou recarregar o PDV, ele retoma o acompanhamento
   automaticamente. **Pausar som** salva sua escolha de deixar o som desligado.
2. Se o navegador bloquear a reprodução, aparece **Liberar som**. Um clique
   normal no PDV ou nesse botão libera o áudio quando o navegador permitir.
   Mesmo com áudio bloqueado, o acompanhamento visual e a impressão continuam.
   Mantenha essa aba aberta, com o computador acordado, volume ligado e sem
   silenciar o site. Sair da gestão interrompe os avisos, preservando a preferência.
3. O painel consulta novos pedidos aproximadamente a cada cinco segundos quando
   a conexão e o navegador estão funcionando normalmente. Pedidos do site,
   WhatsApp e PDV usam o mesmo acompanhamento. Importações iFood de teste
   mantêm a identificação de teste na comanda.

O toque é próprio do Sahara, com notas curtas repetidas para chamar a atenção.
O navegador comum pode exigir um clique para liberar o áudio. Abas suspensas, computador
desligado e aplicativo fechado não conseguem tocar. Não há serviço local de
impressão instalado por essa atualização.

## Abrir no Windows com som automático

Baixe [Abrir-PDV-Som-Automatico.cmd](https://raw.githubusercontent.com/saharaesfihas-card/saharaesfihas/main/downloads/Abrir-PDV-Som-Automatico.cmd)
e use esse atalho para abrir o PDV. Ele inicia uma janela exclusiva do Chrome
com `--autoplay-policy=no-user-gesture-required`, que permite retomar áudio sem
um novo clique de liberação. Faça login na primeira abertura. O atalho não
fecha outras janelas e não altera suas configurações habituais do Chrome.
Se fechou o PDV, abra novamente por esse atalho. Se pausou o som pelo painel,
ative-o outra vez. Volume físico, site silenciado e computador dormindo ainda
impedem ouvir o aviso. O atalho foi preparado no Linux e não executado no seu PC.

No app do PDV instalado pelo Chrome/Edge, o painel recebe a atualização online
e conserva a preferência no perfil em que foi instalado; não é necessário
reinstalar esse app. O app também está sujeito à política de áudio do navegador.
Para abrir sem um novo clique de liberação, use o atalho de som automático no PC.
Ele mantém seu próprio perfil, então o primeiro acesso pode pedir login novamente.

## Impressão automática

Enquanto não tiver impressora, use o som e consulte a comanda pelo pedido.
Quando instalar a impressora, defina-a como padrão no Windows e marque
**Imprimir novos pedidos automaticamente · 2 vias** no PDV.

No navegador comum, cada pedido abre uma janela de impressão com as duas vias.
Escolha a impressora, papel de 80 mm ou o tamanho compatível, uma cópia e todas
as páginas. O PDV não recebe confirmação física de impressão, falta de papel
ou cancelamento da janela. Se necessário, use **Imprimir comanda** novamente.

## Localizar a Knup no Windows

O PDV roda no navegador e não pode listar diretamente os dispositivos Bluetooth
do computador. O Chrome envia a comanda para a impressora padrão do Windows;
por isso, primeiro confirme que a Knup está instalada no próprio Windows:

1. Abra **Configurações do Windows → Bluetooth e dispositivos → Impressoras e
   scanners**. Localize a Knup 605 pelo nome que o Windows atribuiu. Se ela não
   estiver na lista, use **Adicionar dispositivo** e conclua o pareamento; o
   PIN informado para este aparelho é `7711`.
2. Abra a impressora na lista e escolha **Definir como padrão**. Em seguida,
   imprima uma página de teste nas propriedades da impressora. Se a página de
   teste já imprime, não reinstale o driver: apenas defina essa impressora como
   padrão.
3. Nas preferências de impressão do driver, escolha o tamanho do rolo que está
   carregado. Faça uma comanda manual pelo PDV para conferir alinhamento e
   corte antes de ativar a impressão automática.
4. Para dispensar a janela de impressão em cada pedido, abra o PDV pelo atalho
   **Abrir-PDV-Impressao-Direta.cmd** descrito abaixo. O atalho usa a impressora
   padrão do Windows e não escolhe uma impressora por Bluetooth.

Se a Knup imprime a página de teste mas não aparece como padrão nas preferências
do Chrome, feche o Chrome, abra o atalho direto e confira novamente. Em Windows,
o navegador só consegue enviar a comanda às filas de impressão que o sistema já
instalou.

## Imprimir diretamente no Chrome do Windows

Depois de instalar e testar a impressora padrão:

1. Baixe [Abrir-PDV-Impressao-Direta.cmd](https://raw.githubusercontent.com/saharaesfihas-card/saharaesfihas/main/downloads/Abrir-PDV-Impressao-Direta.cmd).
2. Abra o arquivo. Ele inicia o Chrome em um perfil exclusivo do PDV com
   `--kiosk-printing` e áudio automático, sem fechar suas outras janelas.
3. Faça login nessa janela e configure uma impressão manual: impressora correta,
   tamanho do papel, todas as páginas e **uma cópia**. Confira as duas vias.
4. Confira o indicador **som ativo** e marque a impressão automática nessa janela.

Se já tinha uma janela de impressão direta aberta com o atalho antigo, feche
somente essa janela e abra o atalho atualizado para carregar a opção de áudio.

O atalho somente abre o navegador; não instala drivers, não altera pedidos nem
envia mensagens. A impressão direta depende do Chrome e da configuração da
impressora. Foi preparado neste ambiente Linux e não foi executado no seu PC.
O corte entre as vias depende do driver e da impressora.

## Duplicação e reconexão

- Ative os avisos em uma aba do computador responsável por receber os pedidos.
  O mesmo navegador mantém apenas uma aba ativa para evitar repetição;
  as demais aguardam e assumem quando a aba ativa parar.
  Outro navegador ou outro PC tem um controle separado; ative impressão em
  apenas um deles se quiser somente duas vias por pedido.
- Ao ativar, pedidos já existentes são usados como ponto de partida e não
  tocam nem imprimem retroativamente. Atualizar a situação ou repetir a
  confirmação de um pedido não gera uma nova chegada.
- Se a conexão cair com a aba aberta, o acompanhamento conserva seu ponto de
  leitura e confere os pedidos pendentes ao reconectar, em ordem de chegada.
- Comandas são preparadas uma por vez. Sair da gestão interrompe os avisos.

## Verificação sem impressora real

```sh
python -m unittest discover -s server/tests -q
PYTHON=.venv/bin/python node tests/admin.spec.cjs
PYTHON=.venv/bin/python node tests/order-alerts.spec.cjs
```

Os testes usam banco temporário, navegador local, áudio Web Audio e chamadas de
impressão interceptadas. Nenhum pedido, mensagem ou impressão de produção é
criado. Hardware, volume físico e impressão no Windows precisam ser conferidos
no computador da loja.
