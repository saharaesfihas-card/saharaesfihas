"""WhatsApp: Evolution API por QR ou Cloud API, com fila privada persistente."""
from contextlib import asynccontextmanager
import asyncio
from datetime import datetime
import hashlib
import hmac
import json
import os
import re
import threading
import time
from typing import Literal
from types import SimpleNamespace
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request as URLRequest
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from .core import db, require_admin, utcnow
from . import evolution, whatsapp_ai, whatsapp_checkout

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
                  SAHARA_EVOLUTION_EXPECTED_NUMBER=os.environ.get('SAHARA_EVOLUTION_EXPECTED_NUMBER', evolution.DEFAULT_NUMBER).strip(),
                  menu=os.environ.get('SAHARA_PUBLIC_URL', 'https://sahara-esfihas-pdv.onrender.com').rstrip('/') + '/index.html')
    required = evolution.REQUIRED if values['provider'] == 'evolution' else REQUIRED
    valid = evolution.valid_config(values) if values['provider'] == 'evolution' else bool(
        values['provider'] == 'meta' and re.fullmatch(r'v\d+\.\d+', version)
        and values['SAHARA_WHATSAPP_PHONE_NUMBER_ID'].isdigit())
    values['ready'] = values['enabled'] and all(values[key] for key in required) and valid and evolution.valid_url(values['menu'])
    return values


def evolution_fingerprint(cfg):
    """The cache belongs to this exact deployment configuration, not just a name."""
    keys = evolution.REQUIRED + ('SAHARA_EVOLUTION_EXPECTED_NUMBER', 'menu', 'provider', 'enabled')
    return hashlib.sha256(json.dumps({key: cfg[key] for key in keys}, sort_keys=True).encode()).hexdigest()


def cache_evolution_connection(conn, cfg, state):
    put_setting(conn, 'evolution_connection', state)
    put_setting(conn, 'evolution_connection_at', utcnow())
    put_setting(conn, 'evolution_connection_fingerprint', evolution_fingerprint(cfg))


def verified_evolution_connection(conn, cfg):
    if (not cfg['ready'] or setting(conn, 'evolution_connection') != 'open'
            or setting(conn, 'evolution_connection_fingerprint') != evolution_fingerprint(cfg)):
        return False
    try:
        checked = datetime.fromisoformat(setting(conn, 'evolution_connection_at')).timestamp()
        return 0 <= time.time() - checked <= 30
    except (ValueError, TypeError):
        return False


def phone(value, *, international=False):
    digits = re.sub(r'\D', '', str(value))
    if len(digits) in (10, 11) and not international and not str(value).lstrip().startswith('+'):
        digits = '55' + digits
    return evolution.canonical_phone(digits)


def contact_identity(conn, recipient):
    """Read legacy 8-digit mobile contacts conservatively, without losing consent."""
    aliases = evolution.phone_aliases(recipient)
    if not aliases:
        return None
    placeholders = ','.join('?' for _ in aliases)
    row = conn.execute(f'''SELECT MAX(last_inbound) AS last_inbound,
        MAX(human_until) AS human_until,MAX(opted_out) AS opted_out
        FROM whatsapp_contacts WHERE phone IN ({placeholders})''', aliases).fetchone()
    return dict(row) if row and row['last_inbound'] is not None else None


def update_contact_identity(conn, recipient, column, value):
    if column not in ('human_until', 'opted_out'):
        raise ValueError('Campo de contato inválido.')
    aliases = evolution.phone_aliases(recipient)
    if not aliases:
        return 0
    if column == 'human_until' and value > 0:
        previous = contact_identity(conn, recipient)
        value = max(value, previous['human_until'] if previous else 0)
    placeholders = ','.join('?' for _ in aliases)
    expression = f'MAX({column},?)' if column == 'human_until' and value > 0 else '?'
    return conn.execute(f'UPDATE whatsapp_contacts SET {column}={expression} WHERE phone IN ({placeholders})',
                        (value, *aliases)).rowcount


def order_phone_aliases(recipient):
    aliases = evolution.phone_aliases(recipient)
    # Orders entered with a Brazilian DDD are stored without the country code.
    national = tuple(number[2:] for number in aliases if number.startswith('55') and len(number) in (12, 13))
    return aliases + national


def conversations(conn):
    """Present one conversation while retaining historical rows for either JID."""
    result, seen = [], set()
    for row in conn.execute('SELECT phone FROM whatsapp_contacts ORDER BY last_inbound DESC LIMIT 100').fetchall():
        recipient = evolution.canonical_phone(row['phone'])
        if not recipient or recipient in seen:
            continue
        seen.add(recipient)
        contact = contact_identity(conn, recipient)
        aliases = evolution.phone_aliases(recipient)
        placeholders = ','.join('?' for _ in aliases)
        message = conn.execute(f'''SELECT body FROM whatsapp_inbound WHERE phone IN ({placeholders})
            ORDER BY created_at DESC,rowid DESC LIMIT 1''', aliases).fetchone()
        result.append({'phone': recipient, **contact, 'last_message': message['body'] if message else None})
        if len(result) == 50:
            break
    return result


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
    whatsapp_ai.initialize(conn)
    whatsapp_checkout.initialize(conn)


def setting(conn, key, default=''):
    row = conn.execute('SELECT value FROM whatsapp_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def put_setting(conn, key, value):
    conn.execute('INSERT INTO whatsapp_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def queue(conn, event, recipient, kind, body, order_id=None, status=None):
    recipient = evolution.canonical_phone(recipient)
    if not recipient:
        return
    conn.execute('''INSERT OR IGNORE INTO whatsapp_outbox
        (id,event_key,phone,kind,body,order_id,order_status,created_at,updated_at,provider) VALUES(?,?,?,?,?,?,?,?,?,?)''',
        (uuid4().hex, event, recipient, kind, body[:4096], order_id, status, utcnow(), utcnow(), config()['provider']))


def order_notice(conn, order):
    if not config()['ready'] or not order['whatsapp_opt_in']:
        return
    recipient = phone(order['customer_phone'])
    contact = contact_identity(conn, recipient)
    if contact and contact['opted_out']:
        return
    text = f"Sahara: seu pedido #{order['id']} está {LABELS.get(order['status'], order['status'])}."
    queue(conn, f"order:{order['id']}:{order['status']}", recipient, 'order', text, order['id'], order['status'])


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.lower()) if not unicodedata.combining(c)).strip()


def conversational_welcome():
    return ('Olá! Sou o atendimento automático da Sahara. Posso te ajudar a escolher esfihas, '
            'consultar preços, horários e acompanhar seu pedido.\n'
            'O que você gostaria de pedir hoje? Para montar o carrinho aqui, envie PEDIR. Para falar com a equipe, envie ATENDENTE.')


def answer(conn, recipient, text, allow_ai=False, conversational=False):
    normalized = normalize(text)
    now = int(time.time())
    if normalized in ('parar', 'sair', 'stop', 'cancelar avisos'):
        update_contact_identity(conn, recipient, 'opted_out', 1)
        aliases = evolution.phone_aliases(recipient)
        placeholders = ','.join('?' for _ in aliases)
        conn.execute(f"UPDATE whatsapp_outbox SET status='skipped',error='Avisos desativados pelo cliente.',updated_at=? WHERE phone IN ({placeholders}) AND kind='order' AND status IN ('pending','blocked')", (utcnow(), *aliases))
        return 'Os avisos automáticos foram desativados. Para voltar a receber, envie ATIVAR AVISOS.'
    if normalized == 'ativar avisos':
        update_contact_identity(conn, recipient, 'opted_out', 0)
        aliases = order_phone_aliases(recipient)
        placeholders = ','.join('?' for _ in aliases)
        conn.execute(f"UPDATE orders SET whatsapp_opt_in=1 WHERE customer_phone IN ({placeholders}) AND status NOT IN ('delivered','cancelled')", aliases)
        return 'Avisos dos seus pedidos ativos autorizados. Para desativar, envie PARAR.'
    cart = whatsapp_checkout.load(conn, recipient)
    collecting = cart and not cart['expired'] and cart['stage'] not in ('completed', 'cancelled', 'items', 'review')
    if (normalized == '5' and not collecting) or normalized in ('atendente', 'humano') or re.search(r'\b(atendente|humano|reclamacao)\b', normalized) or ((allow_ai or cart) and re.search(r'\b(alergia|alergico|alergica|gluten|lactose|ingredientes|intolerancia|restricao alimentar)\b', normalized)):
        update_contact_identity(conn, recipient, 'human_until', now + 86400)
        return 'Sua conversa ficou disponível para a equipe no PDV. O atendimento automático está pausado. Atendemos de segunda a domingo, das 18h às 23h. Para voltar ao menu automático, envie MENU.'
    contact = contact_identity(conn, recipient)
    if normalized == 'menu':
        update_contact_identity(conn, recipient, 'human_until', 0)
    elif contact and contact['human_until'] > now:
        return None
    natural = allow_ai or conversational
    if natural and re.fullmatch(r'(oi|ola|bom dia|boa tarde|boa noite)[\s!.,?]*', normalized):
        return conversational_welcome()
    if natural and re.fullmatch(r'(obrigad[oa]|muito obrigad[oa]|valeu)[\s!.,?]*', normalized):
        return 'Por nada! Se precisar de mais alguma coisa para o seu pedido, é só me dizer. Para falar com a equipe, envie ATENDENTE.'
    if normalized not in ('menu', 'status', 'status do pedido', 'consultar pedido'):
        try:
            checkout_reply = whatsapp_checkout.handle(conn, recipient, text)
        except ValueError as error:
            checkout_reply = str(error) + ' Para recomeçar os itens, envie LIMPAR CARRINHO.'
        if checkout_reply is not None:
            return checkout_reply
        if cart and not cart['expired'] and cart['stage'] == 'items':
            if allow_ai:
                return whatsapp_ai.PENDING
            return 'Não identifiquei os itens com segurança. Use o nome do cardápio, por exemplo: 2 carne e 1 queijo. Para ajuda, envie ATENDENTE.'
    if normalized == '3' or re.search(r'\b(horario|abre|fecha|funciona|aberto)\b', normalized):
        return 'Atendemos somente por delivery, de segunda a domingo, das 18h às 23h, no horário de Maringá.'
    if normalized == '4' or re.search(r'\b(pix|pagamento|pagar|cartao|dinheiro)\b', normalized):
        return 'Aceitamos Pix, dinheiro, cartão de crédito e débito. Os dados do Pix e a confirmação do pagamento são combinados com a equipe. Envie ATENDENTE para falar com a loja.'
    new_request = allow_ai and re.search(r'\b(novo pedido|(?:fazer|montar|criar|fechar)\s+(?:um |meu |o )?pedido)\b', normalized)
    if not new_request and (normalized == '2' or re.search(r'\b(status|pedido|solicitacao|andamento)\b', normalized)):
        # Nunca confia no número ou ID escritos no texto: a identidade vem do webhook assinado.
        matches = re.findall(r'\b[0-9a-f]{32}\b', normalized)
        aliases = order_phone_aliases(recipient)
        placeholders = ','.join('?' for _ in aliases)
        rows = conn.execute(f'SELECT * FROM orders WHERE customer_phone IN ({placeholders}) ORDER BY created_at DESC LIMIT 10',
                            aliases).fetchall()
        order = next((row for row in rows if row['id'] in matches), None) if matches else (rows[0] if rows else None)
        if order:
            return f"Seu pedido #{order['id']} está {LABELS.get(order['status'], order['status'])}. Para dúvidas ou alterações, envie ATENDENTE. Para autorizar avisos de andamento, envie ATIVAR AVISOS."
        return 'Não encontrei um pedido associado ao seu número de WhatsApp. Informe esse número ao finalizar no cardápio ou envie ATENDENTE.'
    if allow_ai and normalized not in ('1', 'menu', 'oi', 'ola', 'bom dia', 'boa tarde', 'boa noite'):
        return whatsapp_ai.PENDING
    if normalized == '1' or re.search(r'\b(cardapio|comprar|esfiha|shawarma|pedir)\b', normalized):
        return 'Escolha os produtos e finalize seu pedido no cardápio: ' + config()['menu'] + '\nO pedido é registrado no PDV pelo cardápio. Para atendimento pela equipe, envie ATENDENTE.'
    if natural and normalized != 'menu':
        return conversational_welcome()
    return ('Olá! Sou o atendimento automático da Sahara.\n1 — Cardápio e novo pedido\n2 — Consultar meu pedido\n'
            '3 — Horários\n4 — Pagamentos\n5 — Falar com a equipe\nResponda com o número da opção. Para desativar avisos, envie PARAR.')


def respond_to_inbound(conn, identifier, recipient, text):
    before = whatsapp_checkout.load(conn, recipient)
    reply = answer(conn, recipient, text, allow_ai=whatsapp_ai.config()['ready']) if text else answer(conn, recipient, 'atendente')
    if reply is whatsapp_ai.PENDING:
        whatsapp_ai.enqueue(conn, identifier, recipient, config()['provider'])
    elif reply:
        cart = whatsapp_checkout.load(conn, recipient)
        contact = contact_identity(conn, recipient)
        checkout = cart and ((before and before['stage'] not in ('completed', 'cancelled'))
                            or not before or before['id'] != cart['id']
                            or normalize(text) in ('carrinho', 'confirmar pedido')) and not (contact and contact['human_until'] > int(time.time()))
        if checkout:
            conn.execute('INSERT OR IGNORE INTO whatsapp_checkout_events VALUES(?)', (identifier,))
        queue(conn, 'reply:' + identifier, recipient, 'checkout_reply' if checkout else 'reply', reply)


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
                        recipient, key = phone(message.get('from', ''), international=True), message.get('id')
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
                        respond_to_inbound(conn, key, recipient, text)
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
        checked_state = None
        if event == 'connection.update' and isinstance(data, dict) and data.get('state') == 'open':
            # A signed event still cannot establish which number owns the session.
            try:
                checked_state = await asyncio.to_thread(evolution.connection, cfg)
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, AttributeError, TypeError):
                checked_state = 'unavailable'
        with db(request) as conn:
            if event == 'connection.update':
                state = data.get('state')
                if state in ('open', 'close', 'connecting'):
                    cache_evolution_connection(conn, cfg, checked_state if state == 'open' else state)
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
                    respond_to_inbound(conn, identifier, recipient, text)
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
            cache_evolution_connection(conn, cfg, state)
        return {'state': state, 'connected': state == 'open', 'error': (
            'A instância está conectada a outro número. Conecte o WhatsApp da loja configurado no servidor.'
            if state == 'wrong_number' else '')}
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, AttributeError, TypeError) as error:
        state = 'not_created' if isinstance(error, HTTPError) and error.code == 404 else 'unavailable'
        with db(request) as conn:
            cache_evolution_connection(conn, cfg, state)
        return {'state': state, 'connected': False, 'error': ('' if state == 'not_created' else
                'Não foi possível consultar a Evolution API. Confira o serviço, a URL e a chave no Render.')}


@router.post('/api/admin/whatsapp/ai/check', dependencies=[Depends(require_admin)])
def check_ai(request: Request):
    return whatsapp_ai.check_configuration(request)


class AITest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')
    scenario: Literal['products', 'cart'] = 'products'


@router.post('/api/admin/whatsapp/ai/test', dependencies=[Depends(require_admin)])
def test_ai(body: AITest, request: Request):
    return whatsapp_ai.test_generation(request, body.idempotency_key, body.scenario)


@router.get('/api/admin/whatsapp/ai/models', dependencies=[Depends(require_admin)])
def ai_models(request: Request):
    return whatsapp_ai.available_models(request)


class AIModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    model: str = Field(min_length=1, max_length=80, pattern='^' + whatsapp_ai.MODEL_PATTERN + '$')


@router.post('/api/admin/whatsapp/ai/model', dependencies=[Depends(require_admin)])
def ai_model(body: AIModel, request: Request):
    return whatsapp_ai.choose_model(request, body.model)


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
        cache_evolution_connection(conn, cfg, result['state'])
    return result


def payload(conn, row, cfg):
    contact = contact_identity(conn, row['phone'])
    if row['kind'] in ('ai_reply', 'checkout_reply') and contact and contact['human_until'] > int(time.time()):
        return None, 'skipped', 'Atendimento automático pausado para a equipe.'
    if row['kind'] in ('ai_reply', 'checkout_reply') and int(time.time()) - datetime.fromisoformat(row['created_at']).timestamp() > 86400:
        return None, 'skipped', 'Resposta de IA expirada.'
    if row['kind'] == 'order':
        order = conn.execute('SELECT whatsapp_opt_in,status FROM orders WHERE id=?', (row['order_id'],)).fetchone()
        if not order or not order['whatsapp_opt_in'] or (contact and contact['opted_out']):
            return None, 'skipped', 'Avisos não autorizados pelo cliente.'
        if order['status'] != row['order_status']:
            return None, 'skipped', 'Etapa superada. O pedido já avançou para outro estado.'
    if cfg['provider'] == 'evolution':
        return {'number': evolution.canonical_phone(row['phone']), 'text': row['body'], 'linkPreview': False}, None, None
    base = {'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': evolution.canonical_phone(row['phone'])}
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
    # Keep bearer credentials on graph.facebook.com even if the service redirects.
    with evolution.open_url(request, timeout=15) as response:
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
        pending = conn.execute("SELECT 1 FROM whatsapp_outbox WHERE status='pending' AND provider=? AND next_attempt<=? LIMIT 1", (cfg['provider'], int(time.time()))).fetchone()
        if not pending:
            return False
        needs_check = cfg['provider'] == 'evolution' and not verified_evolution_connection(conn, cfg)
    if needs_check:
        status = evolution_status(request, cfg)
        if not status['connected']:
            return False
    with db(request) as conn:
        if cfg['provider'] == 'evolution' and not verified_evolution_connection(conn, cfg):
            return False  # No envio, exige identidade recente da configuração atual.
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
    except (URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
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
                if not pending:
                    pending = whatsapp_ai.process_one(app)
            except Exception:
                pending = False  # Mantém a fila no disco; sem publicar dados em logs.
            stop.wait(0.2 if pending else 2)
    with db(SimpleNamespace(app=app)) as conn:
        conn.execute("UPDATE whatsapp_outbox SET status='uncertain',error='Servidor reiniciado durante envio; confira a conversa.',updated_at=? WHERE status='sending'", (utcnow(),))
        whatsapp_ai.recover(conn)
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
        return {'provider': cfg['provider'], 'connection': connection, 'configured': cfg['ready'], 'enabled': cfg['enabled'], 'template_configured': bool(cfg['template']), 'ai': whatsapp_ai.dashboard(conn),
                'webhook_path': '/api/whatsapp/evolution/webhook' if cfg['provider'] == 'evolution' else '/api/whatsapp/webhook',
                'webhook_verified_at': setting(conn, 'webhook_verified_at') if cfg['provider'] == 'meta' else '',
                'last_webhook_at': setting(conn, 'evolution_last_webhook_at' if cfg['provider'] == 'evolution' else 'last_webhook_at'),
                'missing': [key for key in (evolution.REQUIRED if cfg['provider'] == 'evolution' else REQUIRED) if not cfg[key]],
                'messages': [dict(row) for row in conn.execute('SELECT id,phone,kind,body,status,error,created_at FROM whatsapp_outbox WHERE provider=? ORDER BY created_at DESC LIMIT 50', (cfg['provider'],))],
                'conversations': conversations(conn)}


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
            if evolution.canonical_phone(previous['phone']) != recipient or previous['body'] != body.message:
                raise HTTPException(409, 'Identificador já usado em outra resposta.')
            return {'queued': True, 'replayed': True}
        contact = contact_identity(conn, recipient)
        if not contact:
            raise HTTPException(409, 'A conversa precisa ter uma mensagem recebida do cliente.')
        if config()['provider'] == 'meta' and contact['last_inbound'] <= int(time.time()) - 86400:
            raise HTTPException(409, 'O cliente precisa enviar uma nova mensagem para abrir a janela de atendimento de 24 horas.')
        update_contact_identity(conn, recipient, 'human_until', int(time.time()) + 86400)
        queue(conn, 'manual:' + body.idempotency_key, recipient, 'manual', body.message)
    return {'queued': True, 'replayed': False}


@router.post('/api/admin/whatsapp/conversations/{recipient}/resume', dependencies=[Depends(require_admin)])
def resume(recipient: str, request: Request):
    with db(request) as conn:
        if not update_contact_identity(conn, phone(recipient, international=True), 'human_until', 0):
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
