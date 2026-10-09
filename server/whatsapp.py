"""WhatsApp: Evolution API por QR ou Cloud API, com fila privada persistente."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
import re
import threading
import time
from types import SimpleNamespace
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest, urlopen
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from .core import db, require_admin, utcnow
from . import evolution

router = APIRouter()
LABELS = {'new': 'recebido', 'confirmed': 'confirmado', 'preparing': 'em preparo',
          'ready': 'pronto', 'out_for_delivery': 'a caminho', 'delivered': 'entregue', 'cancelled': 'cancelado'}
REQUIRED = ('SAHARA_WHATSAPP_ACCESS_TOKEN', 'SAHARA_WHATSAPP_PHONE_NUMBER_ID',
            'SAHARA_WHATSAPP_APP_SECRET', 'SAHARA_WHATSAPP_VERIFY_TOKEN')
DELIVERY_RANK = {'accepted': 0, 'sent': 1, 'delivered': 2, 'read': 3, 'failed': 4}


def config():
    values = {key: os.environ.get(key, '').strip() for key in REQUIRED + evolution.REQUIRED}
    version = os.environ.get('SAHARA_WHATSAPP_API_VERSION', 'v25.0').strip()
    values.update(enabled=os.environ.get('SAHARA_WHATSAPP_ENABLED') == '1', version=version,
                  provider=os.environ.get('SAHARA_WHATSAPP_PROVIDER', 'evolution').strip(),
                  template=os.environ.get('SAHARA_WHATSAPP_ORDER_TEMPLATE', '').strip(),
                  language=os.environ.get('SAHARA_WHATSAPP_TEMPLATE_LANGUAGE', 'pt_BR').strip(),
                  menu=os.environ.get('SAHARA_PUBLIC_URL', 'https://sahara-esfihas-pdv.onrender.com').rstrip('/') + '/index.html')
    required = evolution.REQUIRED if values['provider'] == 'evolution' else REQUIRED
    valid = evolution.valid_config(values) if values['provider'] == 'evolution' else bool(
        values['provider'] == 'meta' and re.fullmatch(r'v\d+\.\d+', version)
        and values['SAHARA_WHATSAPP_PHONE_NUMBER_ID'].isdigit())
    values['ready'] = values['enabled'] and all(values[key] for key in required) and valid
    return values


def phone(value):
    digits = re.sub(r'\D', '', str(value))
    if len(digits) in (10, 11):
        digits = '55' + digits
    return digits if 10 <= len(digits) <= 15 else ''


def initialize(conn):
    columns = {row[1] for row in conn.execute('PRAGMA table_info(orders)')}
    if 'whatsapp_opt_in' not in columns:
        conn.execute('ALTER TABLE orders ADD COLUMN whatsapp_opt_in INTEGER NOT NULL DEFAULT 0')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS whatsapp_contacts (
        phone TEXT PRIMARY KEY, last_inbound INTEGER NOT NULL, human_until INTEGER NOT NULL DEFAULT 0,
        opted_out INTEGER NOT NULL DEFAULT 0
      );
      CREATE TABLE IF NOT EXISTS whatsapp_inbound (
        id TEXT PRIMARY KEY, phone TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS whatsapp_inbound_phone ON whatsapp_inbound(phone,created_at);
      CREATE TABLE IF NOT EXISTS whatsapp_outbox (
        id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, phone TEXT NOT NULL,
        kind TEXT NOT NULL, body TEXT NOT NULL, order_id TEXT REFERENCES orders(id),
        order_status TEXT, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt INTEGER NOT NULL DEFAULT 0, provider_id TEXT UNIQUE, error TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS whatsapp_queue ON whatsapp_outbox(status,next_attempt);
      CREATE TABLE IF NOT EXISTS whatsapp_receipts (
        provider_id TEXT PRIMARY KEY, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS whatsapp_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    columns = {row[1] for row in conn.execute('PRAGMA table_info(whatsapp_outbox)')}
    if 'provider' not in columns:
        conn.execute("ALTER TABLE whatsapp_outbox ADD COLUMN provider TEXT NOT NULL DEFAULT 'meta'")


def setting(conn, key, default=''):
    row = conn.execute('SELECT value FROM whatsapp_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def put_setting(conn, key, value):
    conn.execute('INSERT INTO whatsapp_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def queue(conn, event, recipient, kind, body, order_id=None, status=None):
    if not recipient:
        return
    conn.execute('''INSERT OR IGNORE INTO whatsapp_outbox
        (id,event_key,phone,kind,body,order_id,order_status,created_at,updated_at,provider) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (uuid4().hex, event, recipient, kind, body[:4096], order_id, status, utcnow(), utcnow(), config()['provider']))


def order_notice(conn, order):
    if not config()['ready'] or not order['whatsapp_opt_in']:
        return
    recipient = phone(order['customer_phone'])
    contact = conn.execute('SELECT * FROM whatsapp_contacts WHERE phone=?', (recipient,)).fetchone()
    if contact and contact['opted_out']:
        return
    text = f"Sahara: seu pedido #{order['id']} está {LABELS.get(order['status'], order['status'])}."
    queue(conn, f"order:{order['id']}:{order['status']}", recipient, 'order', text, order['id'], order['status'])


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(c)).strip()


def answer(conn, recipient, text):
    normalized = normalize(text)
    now = int(time.time())
    if normalized in ('parar', 'sair', 'stop', 'cancelar avisos'):
        conn.execute('UPDATE whatsapp_contacts SET opted_out=1 WHERE phone=?', (recipient,))
        conn.execute("UPDATE whatsapp_outbox SET status='skipped',error='Avisos desativados pelo cliente.',updated_at=? WHERE phone=? AND kind='order' AND status IN ('pending','blocked')", (utcnow(), recipient))
        return 'Os avisos automáticos foram desativados. Para voltar a receber, envie ATIVAR AVISOS.'
    if normalized == 'ativar avisos':
        conn.execute('UPDATE whatsapp_contacts SET opted_out=0 WHERE phone=?', (recipient,))
        conn.execute("UPDATE orders SET whatsapp_opt_in=1 WHERE (customer_phone=? OR customer_phone=?) AND status NOT IN ('delivered','cancelled')", (recipient, recipient[2:] if recipient.startswith('55') else recipient))
        return 'Avisos dos seus pedidos ativos autorizados. Para desativar, envie PARAR.'
    if normalized in ('5', 'atendente', 'humano') or re.search(r'\b(atendente|humano|reclamacao)\b', normalized):
        conn.execute('UPDATE whatsapp_contacts SET human_until=? WHERE phone=?', (now + 86400, recipient))
        return 'Sua conversa ficou disponível para a equipe no PDV. O atendimento automático está pausado. Atendemos de segunda a domingo, das 18h às 23h. Para voltar ao menu automático, envie MENU.'
    contact = conn.execute('SELECT * FROM whatsapp_contacts WHERE phone=?', (recipient,)).fetchone()
    if normalized == 'menu':
        conn.execute('UPDATE whatsapp_contacts SET human_until=0 WHERE phone=?', (recipient,))
    elif contact['human_until'] > now:
        return None
    if normalized == '3' or re.search(r'\b(horario|abre|fecha|funciona|aberto)\b', normalized):
        return 'Atendemos somente por delivery, de segunda a domingo, das 18h às 23h, no horário de Maringá.'
    if normalized == '4' or re.search(r'\b(pix|pagamento|pagar|cartao|dinheiro)\b', normalized):
        return 'Aceitamos Pix, dinheiro, cartão de crédito e débito. Os dados do Pix e a confirmação do pagamento são combinados com a equipe. Envie ATENDENTE para falar com a loja.'
    if normalized == '2' or re.search(r'\b(status|pedido|solicitacao|andamento)\b', normalized):
        # Nunca confia no número ou ID escritos no texto: a identidade vem do webhook assinado.
        matches = re.findall(r'\b[0-9a-f]{32}\b', normalized)
        rows = conn.execute('SELECT * FROM orders WHERE customer_phone=? OR customer_phone=? ORDER BY created_at DESC LIMIT 10',
                            (recipient, recipient[2:] if recipient.startswith('55') else recipient)).fetchall()
        order = next((row for row in rows if row['id'] in matches), None) if matches else (rows[0] if rows else None)
        if order:
            return f"Seu pedido #{order['id']} está {LABELS.get(order['status'], order['status'])}. Para dúvidas ou alterações, envie ATENDENTE. Para autorizar avisos de andamento, envie ATIVAR AVISOS."
        return 'Não encontrei um pedido associado ao seu número de WhatsApp. Informe esse número ao finalizar no cardápio ou envie ATENDENTE.'
    if normalized == '1' or re.search(r'\b(cardapio|comprar|esfiha|shawarma|pedir)\b', normalized):
        return 'Escolha os produtos e finalize seu pedido no cardápio: ' + config()['menu'] + '\nO pedido é registrado no PDV pelo cardápio. Para atendimento pela equipe, envie ATENDENTE.'
    return ('Olá! Sou o atendimento automático da Sahara.\n1 — Cardápio e novo pedido\n2 — Consultar meu pedido\n'
            '3 — Horários\n4 — Pagamentos\n5 — Falar com a equipe\nResponda com o número da opção. Para desativar avisos, envie PARAR.')


@router.get('/api/whatsapp/webhook')
def verify(request: Request):
    cfg = config()
    token = request.query_params.get('hub.verify_token', '')
    if (not cfg['ready'] or cfg['provider'] != 'meta' or request.query_params.get('hub.mode') != 'subscribe' or
            not hmac.compare_digest(token.encode(), cfg['SAHARA_WHATSAPP_VERIFY_TOKEN'].encode())):
        raise HTTPException(403, 'Verificação não autorizada.')
    challenge = request.query_params.get('hub.challenge', '')
    if not challenge or len(challenge) > 512:
        raise HTTPException(400, 'Desafio inválido.')
    with db(request) as conn:
        put_setting(conn, 'webhook_verified_at', utcnow())
    return PlainTextResponse(challenge)


@router.post('/api/whatsapp/webhook')
async def receive(request: Request):
    cfg = config()
    if not cfg['ready'] or cfg['provider'] != 'meta':
        raise HTTPException(503, 'WhatsApp ainda não configurado.')
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(413, 'Solicitação muito grande.')
    expected = 'sha256=' + hmac.new(cfg['SAHARA_WHATSAPP_APP_SECRET'].encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), request.headers.get('x-hub-signature-256', '').encode()):
        raise HTTPException(403, 'Assinatura inválida.')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get('object') != 'whatsapp_business_account':
            raise ValueError
        with db(request) as conn:
            for entry in body.get('entry', []):
                for change in entry.get('changes', []):
                    value = change.get('value', {})
                    if change.get('field') != 'messages' or value.get('metadata', {}).get('phone_number_id') != cfg['SAHARA_WHATSAPP_PHONE_NUMBER_ID']:
                        continue
                    for receipt in value.get('statuses', []):
                        key, status = receipt.get('id'), receipt.get('status')
                        if not isinstance(key, str) or status not in DELIVERY_RANK:
                            continue
                        old = conn.execute('SELECT status FROM whatsapp_receipts WHERE provider_id=?', (key,)).fetchone()
                        if old and old['status'] in ('delivered', 'read') and status == 'failed':
                            continue
                        if old and DELIVERY_RANK[old['status']] >= DELIVERY_RANK[status]:
                            continue
                        error = 'A Meta informou falha na entrega.' if status == 'failed' else ''
                        conn.execute('INSERT INTO whatsapp_receipts VALUES(?,?,?) ON CONFLICT(provider_id) DO UPDATE SET status=excluded.status,error=excluded.error', (key, status, error))
                        conn.execute('UPDATE whatsapp_outbox SET status=?,error=?,updated_at=? WHERE provider_id=?', (status, error, utcnow(), key))
                    for message in value.get('messages', []):
                        recipient, key = phone(message.get('from', '')), message.get('id')
                        if not recipient or not isinstance(key, str) or not key or len(key) > 512:
                            continue
                        timestamp = int(message.get('timestamp', 0))
                        if timestamp <= 0 or timestamp > int(time.time()):
                            continue
                        text = message.get('text', {}).get('body', '') if message.get('type') == 'text' else ''
                        if not isinstance(text, str):
                            raise ValueError
                        inserted = conn.execute('INSERT OR IGNORE INTO whatsapp_inbound VALUES(?,?,?,?)', (key, recipient, text[:4096] or '[Mensagem não textual]', utcnow())).rowcount
                        if not inserted:
                            continue
                        conn.execute('INSERT INTO whatsapp_contacts(phone,last_inbound) VALUES(?,?) ON CONFLICT(phone) DO UPDATE SET last_inbound=MAX(last_inbound,excluded.last_inbound)', (recipient, timestamp))
                        # Eventos antigos são arquivados, sem reabrir a janela de atendimento.
                        if timestamp < int(time.time()) - 86400:
                            continue
                        reply = answer(conn, recipient, text) if text else answer(conn, recipient, 'atendente')
                        if reply:
                            queue(conn, 'reply:' + key, recipient, 'reply', reply)
            put_setting(conn, 'last_webhook_at', utcnow())
    except (ValueError, TypeError, AttributeError, KeyError):
        raise HTTPException(400, 'Evento inválido.') from None
    return {'ok': True}


def record_receipt(conn, identifier, status, error=''):
    old = conn.execute('SELECT status FROM whatsapp_receipts WHERE provider_id=?', (identifier,)).fetchone()
    if old and (DELIVERY_RANK[old['status']] >= DELIVERY_RANK[status] or
                (old['status'] in ('delivered', 'read') and status == 'failed')):
        return
    conn.execute('INSERT INTO whatsapp_receipts VALUES(?,?,?) ON CONFLICT(provider_id) DO UPDATE SET status=excluded.status,error=excluded.error', (identifier, status, error))
    conn.execute('UPDATE whatsapp_outbox SET status=?,error=?,updated_at=? WHERE provider_id=?', (status, error, utcnow(), identifier))


@router.post('/api/whatsapp/evolution/webhook')
async def evolution_webhook(request: Request):
    cfg = config()
    if cfg['provider'] != 'evolution' or not cfg['ready']:
        raise HTTPException(503, 'Evolution API ainda não configurada.')
    if not hmac.compare_digest(request.headers.get('x-sahara-webhook-secret', '').encode(),
                               cfg['SAHARA_EVOLUTION_WEBHOOK_SECRET'].encode()):
        raise HTTPException(403, 'Evento não autorizado.')
    raw = await request.body()
    if len(raw) > 65536:
        raise HTTPException(413, 'Solicitação muito grande.')
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or body.get('instance') != cfg['SAHARA_EVOLUTION_INSTANCE']:
            raise HTTPException(403, 'Instância não autorizada.')
        event = body.get('event', '').lower().replace('_', '.')
        data = body.get('data', {})
        with db(request) as conn:
            if event == 'connection.update':
                state = data.get('state')
                if state in ('open', 'close', 'connecting'):
                    put_setting(conn, 'evolution_connection', state)
                    put_setting(conn, 'evolution_connection_at', utcnow())
            elif event == 'messages.update':
                mapping = {'SERVER_ACK': 'sent', 'DELIVERY_ACK': 'delivered', 'READ': 'read',
                           'PLAYED': 'read', 'ERROR': 'failed', 2: 'sent', 3: 'delivered', 4: 'read', 5: 'read', 0: 'failed'}
                for item in data if isinstance(data, list) else [data]:
                    if item.get('fromMe') is not True and item.get('key', {}).get('fromMe') is not True:
                        continue
                    identifier = item.get('keyId') or item.get('key', {}).get('id')
                    status = mapping.get(item.get('status', item.get('update', {}).get('status')))
                    if isinstance(identifier, str) and identifier and status:
                        record_receipt(conn, 'evolution:' + identifier, status, 'A Evolution informou falha na entrega.' if status == 'failed' else '')
            elif event == 'messages.upsert':
                for item in data if isinstance(data, list) else [data]:
                    key = item.get('key', {})
                    if key.get('fromMe') is not False:
                        continue  # Ignora mensagens próprias: evita respostas em loop.
                    recipient, identifier = evolution.jid_phone(key), key.get('id')
                    if not recipient or not isinstance(identifier, str) or not identifier or len(identifier) > 512:
                        continue
                    timestamp = item.get('messageTimestamp', 0)
                    if isinstance(timestamp, dict):
                        timestamp = timestamp.get('low', 0)
                    timestamp = int(timestamp)
                    if timestamp <= 0 or timestamp > int(time.time()):
                        continue
                    message = item.get('message', {})
                    if message.get('protocolMessage') or message.get('reactionMessage'):
                        continue
                    text = evolution.text_message(message)
                    if not isinstance(text, str):
                        raise ValueError
                    identifier = 'evolution:' + cfg['SAHARA_EVOLUTION_INSTANCE'] + ':' + identifier
                    if not conn.execute('INSERT OR IGNORE INTO whatsapp_inbound VALUES(?,?,?,?)', (identifier, recipient, text[:4096] or '[Mensagem não textual]', utcnow())).rowcount:
                        continue
                    conn.execute('INSERT INTO whatsapp_contacts(phone,last_inbound) VALUES(?,?) ON CONFLICT(phone) DO UPDATE SET last_inbound=MAX(last_inbound,excluded.last_inbound)', (recipient, timestamp))
                    if timestamp < int(time.time()) - 86400:
                        continue  # Não responde ao histórico antigo sincronizado ao parear.
                    reply = answer(conn, recipient, text) if text else answer(conn, recipient, 'atendente')
                    if reply:
                        queue(conn, 'reply:' + identifier, recipient, 'reply', reply)
            put_setting(conn, 'evolution_last_webhook_at', utcnow())
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise HTTPException(400, 'Evento inválido.') from None
    return {'ok': True}


def evolution_status(request, cfg):
    if not cfg['ready']:
        return {'state': 'not_configured', 'connected': False, 'error': ''}
    try:
        state = evolution.connection(cfg)
        with db(request) as conn:
            put_setting(conn, 'evolution_connection', state)
            put_setting(conn, 'evolution_connection_at', utcnow())
        return {'state': state, 'connected': state == 'open', 'error': ''}
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, AttributeError):
        with db(request) as conn:
            put_setting(conn, 'evolution_connection', 'unavailable')
        return {'state': 'unavailable', 'connected': False, 'error': 'Não foi possível consultar a Evolution API. Confira o serviço, a URL e a chave no Render.'}


@router.post('/api/admin/whatsapp/connect', dependencies=[Depends(require_admin)])
def connect_evolution(request: Request):
    cfg = config()
    if cfg['provider'] != 'evolution' or not cfg['ready']:
        raise HTTPException(503, 'Configure a URL, a chave, a instância e o segredo de webhook da Evolution API no Render.')
    try:
        result = evolution.connect(cfg)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, AttributeError, TypeError):
        raise HTTPException(502, 'Não foi possível preparar a conexão. Confira a Evolution API e suas credenciais no Render.') from None
    with db(request) as conn:
        put_setting(conn, 'evolution_connection', result['state'])
        put_setting(conn, 'evolution_connection_at', utcnow())
    return result


def payload(conn, row, cfg):
    contact = conn.execute('SELECT * FROM whatsapp_contacts WHERE phone=?', (row['phone'],)).fetchone()
    if row['kind'] == 'order':
        order = conn.execute('SELECT whatsapp_opt_in FROM orders WHERE id=?', (row['order_id'],)).fetchone()
        if not order or not order['whatsapp_opt_in'] or (contact and contact['opted_out']):
            return None, 'skipped', 'Avisos não autorizados pelo cliente.'
    if cfg['provider'] == 'evolution':
        return {'number': row['phone'], 'text': row['body'], 'linkPreview': False}, None, None
    base = {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': row['phone']}
    if contact and contact['last_inbound'] > int(time.time()) - 86400:
        return base | {'type': 'text', 'text': {'preview_url': False, 'body': row['body']}}, None, None
    if row['kind'] == 'order' and cfg['template']:
        return base | {'type': 'template', 'template': {'name': cfg['template'], 'language': {'code': cfg['language']},
                       'components': [{'type': 'body', 'parameters': [{'type': 'text', 'text': row['order_id']},
                                       {'type': 'text', 'text': LABELS[row['order_status']]}]}]}}, None, None
    return None, 'blocked', 'Fora da janela de 24 horas. Avisos de pedidos precisam de modelo aprovado pela Meta.'


def send(cfg, data):
    """Retorna o ID aceito pela Meta; nunca registra tokens nem respostas brutas."""
    if cfg['provider'] == 'evolution':
        return evolution.send(cfg, data)
    request = URLRequest(f"https://graph.facebook.com/{cfg['version']}/{cfg['SAHARA_WHATSAPP_PHONE_NUMBER_ID']}/messages",
                         data=json.dumps(data).encode(), method='POST',
                         headers={'Authorization': 'Bearer ' + cfg['SAHARA_WHATSAPP_ACCESS_TOKEN'], 'Content-Type': 'application/json'})
    with urlopen(request, timeout=15) as response:
        result = json.load(response)
    identifier = result['messages'][0]['id']
    if not isinstance(identifier, str) or not identifier:
        raise ValueError('Resposta inválida.')
    return identifier


def process_one(app):
    cfg = config()
    if not cfg['ready']:
        return False
    request = SimpleNamespace(app=app)
    with db(request) as conn:
        if cfg['provider'] == 'evolution' and setting(conn, 'evolution_connection') != 'open':
            return False  # Mantém os envios na fila até que a conexão seja confirmada.
        row = conn.execute("SELECT * FROM whatsapp_outbox WHERE status='pending' AND provider=? AND next_attempt<=? ORDER BY created_at,id LIMIT 1", (cfg['provider'], int(time.time()))).fetchone()
        if not row:
            return False
        data, status, error = payload(conn, row, cfg)
        if not data:
            conn.execute('UPDATE whatsapp_outbox SET status=?,error=?,updated_at=? WHERE id=?', (status, error, utcnow(), row['id']))
            return True
        conn.execute("UPDATE whatsapp_outbox SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (utcnow(), row['id']))
    identifier, error, status, next_attempt = None, '', 'accepted', 0
    try:
        identifier = send(cfg, data)
    except HTTPError as exc:
        # 429 é rejeição explícita; falhas ambíguas não são reenviadas automaticamente.
        status = 'pending' if exc.code == 429 and row['attempts'] < 4 else ('uncertain' if exc.code >= 500 else 'failed')
        error = f"{'Evolution API' if cfg['provider'] == 'evolution' else 'Meta'} HTTP {exc.code}. Confira a conexão e a configuração do serviço."
        next_attempt = int(time.time()) + min(60 * 2 ** row['attempts'], 3600)
    except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError):
        status, error = 'uncertain', 'Envio sem confirmação. Confira a conversa antes de reenviar para evitar duplicidade.'
    with db(request) as conn:
        receipt = conn.execute('SELECT * FROM whatsapp_receipts WHERE provider_id=?', (identifier,)).fetchone() if identifier else None
        if receipt:
            status, error = receipt['status'], receipt['error']
        conn.execute('UPDATE whatsapp_outbox SET status=?,provider_id=?,error=?,next_attempt=?,updated_at=? WHERE id=?',
                     (status, identifier, error, next_attempt, utcnow(), row['id']))
    return True


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    def worker():
        while not stop.is_set():
            try:
                pending = process_one(app)
            except Exception:
                pending = False  # Mantém a fila no disco; sem publicar dados em logs.
            stop.wait(0.2 if pending else 2)
    with db(SimpleNamespace(app=app)) as conn:
        conn.execute("UPDATE whatsapp_outbox SET status='uncertain',error='Servidor reiniciado durante envio; confira a conversa.',updated_at=? WHERE status='sending'", (utcnow(),))
    thread = threading.Thread(target=worker, name='sahara-whatsapp', daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=17)


def availability():
    cfg = config()
    return {'id': 'whatsapp', 'label': 'WhatsApp · atendimento e avisos de pedidos', 'available': cfg['ready'],
            'reason': ('Configuração presente. Confira a conexão e os envios na área de WhatsApp.' if cfg['ready'] else
                       'Configure o serviço Evolution API no Render para conectar pelo QR Code.' if cfg['provider'] == 'evolution' else
                       'Ative a API oficial da Meta e configure o número e as credenciais no Render.')}


@router.get('/api/admin/whatsapp', dependencies=[Depends(require_admin)])
def dashboard(request: Request):
    cfg = config()
    connection = evolution_status(request, cfg) if cfg['provider'] == 'evolution' else None
    with db(request) as conn:
        return {'provider': cfg['provider'], 'connection': connection, 'configured': cfg['ready'], 'enabled': cfg['enabled'], 'template_configured': bool(cfg['template']),
                'webhook_path': '/api/whatsapp/evolution/webhook' if cfg['provider'] == 'evolution' else '/api/whatsapp/webhook',
                'webhook_verified_at': setting(conn, 'webhook_verified_at') if cfg['provider'] == 'meta' else '',
                'last_webhook_at': setting(conn, 'evolution_last_webhook_at' if cfg['provider'] == 'evolution' else 'last_webhook_at'),
                'missing': [key for key in (evolution.REQUIRED if cfg['provider'] == 'evolution' else REQUIRED) if not cfg[key]],
                'messages': [dict(row) for row in conn.execute('SELECT id,phone,kind,body,status,error,created_at FROM whatsapp_outbox WHERE provider=? ORDER BY created_at DESC LIMIT 50', (cfg['provider'],))],
                'conversations': [dict(row) for row in conn.execute('''SELECT c.*,
                    (SELECT body FROM whatsapp_inbound i WHERE i.phone=c.phone ORDER BY created_at DESC,rowid DESC LIMIT 1) AS last_message
                    FROM whatsapp_contacts c ORDER BY last_inbound DESC LIMIT 50''')]}


class Reply(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    phone: str = Field(min_length=10, max_length=20)
    message: str = Field(min_length=1, max_length=4096)
    idempotency_key: str = Field(min_length=16, max_length=128)


@router.post('/api/admin/whatsapp/reply', dependencies=[Depends(require_admin)], status_code=202)
def manual_reply(body: Reply, request: Request):
    if not config()['ready']:
        raise HTTPException(503, 'Configure o WhatsApp no Render antes de enviar.')
    recipient = phone(body.phone)
    with db(request) as conn:
        previous = conn.execute('SELECT * FROM whatsapp_outbox WHERE event_key=?', ('manual:' + body.idempotency_key,)).fetchone()
        if previous:
            if previous['phone'] != recipient or previous['body'] != body.message:
                raise HTTPException(409, 'Identificador já usado em outra resposta.')
            return {'queued': True, 'replayed': True}
        contact = conn.execute('SELECT * FROM whatsapp_contacts WHERE phone=?', (recipient,)).fetchone()
        if not contact:
            raise HTTPException(409, 'A conversa precisa ter uma mensagem recebida do cliente.')
        if config()['provider'] == 'meta' and contact['last_inbound'] <= int(time.time()) - 86400:
            raise HTTPException(409, 'O cliente precisa enviar uma nova mensagem para abrir a janela de atendimento de 24 horas.')
        conn.execute('UPDATE whatsapp_contacts SET human_until=? WHERE phone=?', (int(time.time()) + 86400, recipient))
        queue(conn, 'manual:' + body.idempotency_key, recipient, 'manual', body.message)
    return {'queued': True, 'replayed': False}


@router.post('/api/admin/whatsapp/conversations/{recipient}/resume', dependencies=[Depends(require_admin)])
def resume(recipient: str, request: Request):
    with db(request) as conn:
        if not conn.execute('UPDATE whatsapp_contacts SET human_until=0 WHERE phone=?', (phone(recipient),)).rowcount:
            raise HTTPException(404, 'Conversa não encontrada.')
    return {'ok': True}


@router.post('/api/admin/whatsapp/messages/{identifier}/retry', dependencies=[Depends(require_admin)])
def retry(identifier: str, request: Request):
    if not config()['ready']:
        raise HTTPException(503, 'Configure o WhatsApp antes de reenviar.')
    with db(request) as conn:
        row = conn.execute('SELECT * FROM whatsapp_outbox WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, 'Mensagem não encontrada.')
        if row['provider'] != config()['provider']:
            raise HTTPException(409, 'Esta mensagem pertence a outra conexão de WhatsApp.')
        if row['status'] not in ('failed', 'blocked'):
            raise HTTPException(409, 'Esta mensagem não pode ser reenviada automaticamente. Confira a conversa.')
        conn.execute("UPDATE whatsapp_outbox SET status='pending',attempts=0,next_attempt=0,error='',provider_id=NULL,updated_at=? WHERE id=?", (utcnow(), identifier))
    return {'queued': True}
