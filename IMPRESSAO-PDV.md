# Avisos e impressão de pedidos

O PDV pode tocar um aviso de novo pedido e preparar **duas vias idênticas** da
comanda, tanto na impressão automática quanto em **Imprimir comanda**.
As duas vias são páginas separadas de um único trabalho. Deixe o número de
cópias do navegador/driver em **1**, para não sair quatro vias.

## Ativar o toque

1. Entre no PDV e clique em **Ativar som de pedidos**. Você ouvirá uma prévia.
2. Mantenha essa aba aberta, com o computador acordado, o volume ligado e sem
   silenciar o site. Ao abrir ou recarregar o PDV, ative novamente.
3. O painel consulta novos pedidos aproximadamente a cada cinco segundos quando
   a conexão e o navegador estão funcionando normalmente. Pedidos do site,
   WhatsApp e PDV usam o mesmo acompanhamento. Importações iFood de teste
   mantêm a identificação de teste na comanda.

O toque é próprio do Sahara, com notas curtas repetidas para chamar a atenção.
O navegador exige um clique para liberar o áudio. Abas suspensas, computador
desligado e aplicativo fechado não conseguem tocar. Não há serviço local de
impressão instalado por essa atualização.

## Impressão automática

Enquanto não tiver impressora, use o som e consulte a comanda pelo pedido.
Quando instalar a impressora, defina-a como padrão no Windows e marque
**Imprimir novos pedidos automaticamente · 2 vias** no PDV.

No navegador comum, cada pedido abre uma janela de impressão com as duas vias.
Escolha a impressora, papel de 80 mm ou o tamanho compatível, uma cópia e todas
as páginas. O PDV não recebe confirmação física de impressão, falta de papel
ou cancelamento da janela. Se necessário, use **Imprimir comanda** novamente.

## Imprimir diretamente no Chrome do Windows

Depois de instalar e testar a impressora padrão:

1. Baixe [Abrir-PDV-Impressao-Direta.cmd](https://raw.githubusercontent.com/saharaesfihas-card/saharaesfihas/main/downloads/Abrir-PDV-Impressao-Direta.cmd).
2. Abra o arquivo. Ele inicia o Chrome em um perfil exclusivo do PDV com
   `--kiosk-printing`, sem fechar suas outras janelas.
3. Faça login nessa janela e configure uma impressão manual: impressora correta,
   tamanho do papel, todas as páginas e **uma cópia**. Confira as duas vias.
4. Ative o som e marque a impressão automática nessa janela.

O atalho somente abre o navegador; não instala drivers, não altera pedidos nem
envia mensagens. A impressão direta depende do Chrome e da configuração da
impressora. Foi preparado neste ambiente Linux e não foi executado no seu PC.
O corte entre as vias depende do driver e da impressora.

## Duplicação e reconexão

- Ative os avisos em uma aba do computador responsável por receber os pedidos.
  O mesmo navegador bloqueia a ativação em outra aba para evitar repetição.
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
