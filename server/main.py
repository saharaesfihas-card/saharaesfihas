"""API e painel privado da operação Sahara; integrações externas não são simuladas."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
from typing import Literal
import uuid
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator

from . import core, whatsapp
from .core import COOKIE, ROOT, db, order_dict, require_admin, utcnow, verify_password

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, allow_inf_nan=False)

class Customer(Model):
    name: str = Field(default='', max_length=100)
    phone: str = Field(default='', max_length=30)
    marketing_opt_in: bool = False
    whatsapp_opt_in: bool = False

    @model_validator(mode='after')
    def phone_number(self):
        if self.phone:
            if any(not (c.isdigit() or c in '+ ()-.') for c in self.phone):
                raise ValueError('Informe um telefone válido.')
            self.phone = ''.join(c for c in self.phone if c.isdigit())
            if not 10 <= len(self.phone) <= 15:
                raise ValueError('Informe um telefone com DDD.')
        if self.whatsapp_opt_in and not self.phone:
            raise ValueError('Informe seu número de WhatsApp para receber avisos do pedido.')
        return self

class Location(Model):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0)
    url: str | None = Field(default=None, max_length=300)

class Delivery(Model):
    street: str = Field(default='', max_length=160)
    number: str = Field(min_length=1, max_length=20)
    neighborhood: str = Field(default='', max_length=100)
    complement: str = Field(default='', max_length=160)
    location: Location | None = None

    @model_validator(mode='after')
    def complete_address(self):
        if self.location is None and (not self.street or not self.neighborhood):
            raise ValueError('Informe rua e bairro ou confirme um ponto de localização.')
        return self

class Item(Model):
    id: str = Field(min_length=1, max_length=100)
    quantity: StrictInt = Field(ge=1, le=99)

class OrderInput(Model):
    items: list[Item] = Field(min_length=1, max_length=100)
    customer: Customer = Field(default_factory=Customer)
    delivery: Delivery
    payment_method: Literal['', 'A combinar', 'Pix', 'Dinheiro', 'Cartão de crédito', 'Cartão de débito'] = ''
    notes: str = Field(default='', max_length=500)
    requested_for: AwareDatetime | None = None
    coupon_code: str | None = Field(default=None, max_length=40)
    idempotency_key: str = Field(min_length=16, max_length=128)

class StatusInput(Model):
    status: Literal['confirmed', 'preparing', 'ready', 'out_for_delivery', 'delivered', 'cancelled']

class PaymentInput(Model):
    status: Literal['paid']

class Login(Model):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=False)
    password: str = Field(min_length=1, max_length=1024)

class EventInput(Model):
    name: Literal['view', 'checkout_started']

TRANSITIONS = {
    'new': {'confirmed', 'preparing', 'cancelled'},
    'confirmed': {'preparing', 'cancelled'},
    'preparing': {'ready', 'cancelled'},
    'ready': {'out_for_delivery', 'cancelled'},
    'out_for_delivery': {'delivered', 'cancelled'},
    'delivered': set(), 'cancelled': set()
}

def schedule(value):
    if value is None:
        return None
    now = datetime.now(timezone.utc)
    local = value.astimezone(ZoneInfo('America/Sao_Paulo'))
    if value <= now or local.date() > now.astimezone(ZoneInfo('America/Sao_Paulo')).date() + timedelta(days=7):
        raise HTTPException(422, 'Escolha um horário futuro nos próximos sete dias.')
    if not (18 <= local.hour < 23 or (local.hour == 23 and local.minute == 0)) or local.second or local.microsecond:
        raise HTTPException(422, 'Agende entre 18h e 23h, no horário de Maringá.')
    return value.astimezone(timezone.utc).isoformat()

def create_app(data_dir=None, admin_password_hash=None, secure_cookie=None):
    from . import marketing, operations
    data_dir = Path(data_dir or os.environ.get('SAHARA_DATA_DIR', ROOT.parent / '.sahara-system-data')).resolve()
    if data_dir == ROOT or ROOT in data_dir.parents:
        raise ValueError('Guarde o banco e os dados privados fora do diretório publicado do site.')
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path = data_dir / 'sahara.sqlite3'
    app = FastAPI(title='Sahara · Gestão delivery', docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=whatsapp.lifespan)
    app.state.db_path = db_path
    app.state.admin_password_hash = admin_password_hash if admin_password_hash is not None else os.environ.get('SAHARA_ADMIN_PASSWORD_HASH', '')
    app.state.secure_cookie = secure_cookie if secure_cookie is not None else os.environ.get('SAHARA_COOKIE_SECURE', '1') != '0'
    core.DEFAULT_DB = db_path
    catalog = json.loads(subprocess.check_output(['node', str(ROOT / 'server' / 'export-catalog.cjs')], text=True))
    with sqlite3.connect(db_path) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(core.SCHEMA)
        operations.initialize(conn)
        marketing.initialize(conn)
        whatsapp.initialize(conn)
        for product in catalog:
            conn.execute('INSERT INTO products(id,name,category,price_cents) VALUES(?,?,?,?) '
                         'ON CONFLICT(id) DO UPDATE SET name=excluded.name,category=excluded.category,price_cents=excluded.price_cents',
                         (product['id'], product['name'], product['category'], product['priceCents']))
        conn.commit()
    os.chmod(db_path, 0o600)

    allowed = [s.strip() for s in os.environ.get('SAHARA_ALLOWED_ORIGINS', 'https://saharaesfihas-card.github.io').split(',') if s.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=allowed, allow_credentials=False,
                       allow_methods=['GET', 'POST', 'OPTIONS'], allow_headers=['Content-Type'])

    @app.middleware('http')
    async def response_headers(request, call_next):
        if request.headers.get('content-length', '').isdigit() and int(request.headers['content-length']) > 65536:
            return JSONResponse({'detail': 'Solicitação muito grande.'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api/') or request.url.path in ['/admin.html', '/system-config.js']:
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_data(request, error):
        return JSONResponse({'detail': 'Confira os dados informados.', 'fields': [
            {'field': '.'.join(map(str, item['loc'])), 'message': item['msg']} for item in error.errors()
        ]}, status_code=422)

    @app.get('/api/health')
    def health():
        return {'ready': True, 'admin_configured': bool(app.state.admin_password_hash),
                'business': {'name': 'Sahara Esfihas e Shawarma', 'delivery_only': True, 'opens': '18:00', 'closes': '23:00', 'timezone': 'America/Sao_Paulo'}}

    @app.get('/api/catalog')
    def products(request: Request):
        with db(request) as conn:
            return {'products': [dict(row) for row in conn.execute('SELECT * FROM products ORDER BY category,name')]}

    @app.get('/api/integrations')
    def integrations():
        entries = [
            ('ai', 'Atendimento com inteligência artificial', 'Conectar um provedor de IA e o canal de atendimento.'),
            ('payments', 'Pagamento online com cartão e Pix', 'Conectar provedor de pagamentos e confirmação por webhook.'),
            ('fiscal', 'Emissão de notas fiscais', 'Configurar dados fiscais e um emissor autorizado.'),
            ('ifood', 'iFood e Entrega Fácil', 'Habilitar acesso à API da conta da loja no iFood.'),
            ('f360', 'Gestão financeira F360', 'Conectar conta e integração autorizada da F360.'),
            ('ads', 'Meta Ads, Google Ads, Analytics e GTM', 'Informar os identificadores e configurar as contas da loja.'),
            ('printers', 'Impressão automática em múltiplas impressoras', 'Conectar as impressoras e uma ponte local de impressão. A comanda pode ser impressa pelo navegador.'),
            ('recovery', 'Recuperação automática de carrinho', 'Conectar mensagens oficiais e identificação consentida de clientes.')
        ]
        return {'integrations': [whatsapp.availability()] + [{'id': key, 'label': label, 'available': False, 'reason': reason} for key, label, reason in entries]}

    @app.post('/api/admin/login')
    def login(body: Login, request: Request, response: Response):
        if not app.state.admin_password_hash:
            raise HTTPException(503, 'Defina a senha de administrador antes de ativar a gestão.')
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Entre pelo endereço do painel da loja.')
        actor = hashlib.sha256((request.client.host if request.client else 'unknown').encode()).hexdigest()
        now = int(time.time())
        with db(request) as conn:
            conn.execute('DELETE FROM login_attempts WHERE created_at<?', (now - 900,))
            failures = conn.execute('SELECT COUNT(*) FROM login_attempts WHERE actor=?', (actor,)).fetchone()[0]
            if failures >= 5:
                raise HTTPException(429, 'Muitas tentativas. Aguarde quinze minutos.')
            valid = verify_password(body.password, app.state.admin_password_hash)
            if not valid:
                conn.execute('INSERT INTO login_attempts VALUES(?,?)', (actor, now))
            else:
                conn.execute('DELETE FROM login_attempts WHERE actor=?', (actor,))
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                conn.execute('DELETE FROM sessions WHERE expires_at<=?', (now,))
                conn.execute('INSERT INTO sessions VALUES(?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), csrf, now + 28800))
        if not valid:
            raise HTTPException(401, 'Senha incorreta.')
        response.set_cookie(COOKIE, token, httponly=True, secure=app.state.secure_cookie, samesite='strict', max_age=28800, path='/api/admin')
        return {'csrf_token': csrf}

    @app.get('/api/admin/session')
    def session(admin=Depends(require_admin)):
        return {'authenticated': True, 'csrf_token': admin['csrf']}

    @app.post('/api/admin/logout')
    def logout(request: Request, response: Response, admin=Depends(require_admin)):
        with db(request) as conn:
            conn.execute('DELETE FROM sessions WHERE token_hash=?', (admin['token_hash'],))
        response.delete_cookie(COOKIE, path='/api/admin', secure=app.state.secure_cookie, httponly=True, samesite='strict')
        return {'ok': True}

    def register_order(body, request, source='web'):
        raw = body.model_dump(mode='json')
        # Preserva os identificadores de tentativas anteriores à integração.
        if not raw['customer']['whatsapp_opt_in']:
            raw['customer'].pop('whatsapp_opt_in')
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with db(request) as conn:
            existing = conn.execute('SELECT * FROM orders WHERE idempotency_key=?', (body.idempotency_key,)).fetchone()
            if existing:
                if existing['request_hash'] != fingerprint:
                    raise HTTPException(409, 'Este identificador já foi usado para outro pedido.')
                return {'id': existing['id'], 'status': existing['status'], 'total_cents': existing['total_cents'],
                        'tracking_token': existing['tracking_token'], 'tracking_url': None, 'replayed': True}
            requested_for = schedule(body.requested_for)
            ids = [item.id for item in body.items]
            if len(ids) != len(set(ids)):
                raise HTTPException(422, 'Há produtos repetidos no pedido.')
            items = []
            for item in body.items:
                product = conn.execute('SELECT * FROM products WHERE id=?', (item.id,)).fetchone()
                if not product:
                    raise HTTPException(422, 'Produto não encontrado no cardápio.')
                items.append({'id': item.id, 'name': product['name'], 'quantity': item.quantity, 'price_cents': product['price_cents']})
            customer_id = None
            if body.customer.phone:
                customer = conn.execute('SELECT * FROM customers WHERE phone=?', (body.customer.phone,)).fetchone()
                customer_id = customer['id'] if customer else uuid.uuid4().hex
                if customer:
                    conn.execute('UPDATE customers SET name=CASE WHEN ?<>\'\' THEN ? ELSE name END, '
                                 'marketing_opt_in=CASE WHEN ? THEN 1 ELSE marketing_opt_in END WHERE id=?',
                                 (body.customer.name, body.customer.name, body.customer.marketing_opt_in, customer_id))
                else:
                    conn.execute('INSERT INTO customers(id,name,phone,marketing_opt_in,created_at) VALUES(?,?,?,?,?)',
                                 (customer_id, body.customer.name, body.customer.phone, int(body.customer.marketing_opt_in), utcnow()))
            subtotal = sum(item['quantity'] * item['price_cents'] for item in items)
            code = body.coupon_code.strip().upper() if body.coupon_code else None
            discount = marketing.coupon_discount(conn, code, subtotal, customer_id)
            if not 0 <= discount <= subtotal:
                raise HTTPException(422, 'Desconto inválido.')
            delivery = body.delivery.model_dump(mode='json')
            if body.delivery.location:
                point = body.delivery.location
                delivery['location']['url'] = f'https://www.google.com/maps?q={point.latitude:.6f},{point.longitude:.6f}'
            order_id, tracking = uuid.uuid4().hex, secrets.token_urlsafe(24)
            conn.execute('''INSERT INTO orders(id,created_at,requested_for,customer_id,customer_name,customer_phone,
                delivery_json,items_json,subtotal_cents,discount_cents,total_cents,coupon_code,payment_method,
                tracking_token,idempotency_key,request_hash,source,notes,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (order_id, utcnow(), requested_for, customer_id, body.customer.name, body.customer.phone,
                 json.dumps(delivery, ensure_ascii=False), json.dumps(items, ensure_ascii=False), subtotal, discount,
                 subtotal - discount, code, body.payment_method, tracking, body.idempotency_key, fingerprint, source, body.notes, 'preparing'))
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            operations.apply_stock(conn, order)
            marketing.record_coupon_use(conn, order)
            conn.execute('UPDATE orders SET stock_applied=1,confirmed_at=? WHERE id=?', (utcnow(), order_id))
            conn.execute('INSERT INTO analytics_events VALUES(?,?)', ('order_registered', utcnow()))
            conn.execute('INSERT INTO order_events VALUES(?,?,?,?)', (uuid.uuid4().hex, order_id, 'preparing', utcnow()))
            conn.execute('UPDATE orders SET whatsapp_opt_in=? WHERE id=?', (int(body.customer.whatsapp_opt_in), order_id))
            whatsapp.order_notice(conn, conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())
            return {'id': order_id, 'status': 'preparing', 'total_cents': subtotal - discount, 'tracking_token': tracking,
                    'tracking_url': None, 'replayed': False}

    @app.post('/api/orders', status_code=201)
    def new_order(body: OrderInput, request: Request):
        return register_order(body, request)

    @app.post('/api/admin/orders', status_code=201)
    def manual_order(body: OrderInput, request: Request, admin=Depends(require_admin)):
        return register_order(body, request, 'pdv')

    @app.get('/api/orders/{order_id}/track')
    def track(order_id: str, token: str, request: Request):
        with db(request) as conn:
            row = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not row or not hmac.compare_digest(token.encode('utf-8'), row['tracking_token'].encode('utf-8')):
                raise HTTPException(404, 'Pedido não encontrado.')
            return {'id': row['id'], 'status': row['status'], 'payment_status': row['payment_status'],
                    'total_cents': row['total_cents'], 'requested_for': row['requested_for'], 'items': json.loads(row['items_json'])}

    @app.get('/api/admin/orders')
    def orders(request: Request, status: str | None = None, admin=Depends(require_admin)):
        with db(request) as conn:
            rows = conn.execute('SELECT * FROM orders WHERE (? IS NULL OR status=?) ORDER BY created_at DESC LIMIT 500', (status, status)).fetchall()
            return {'orders': [order_dict(row) for row in rows]}

    @app.patch('/api/admin/orders/{order_id}')
    def change_status(order_id: str, body: StatusInput, request: Request, admin=Depends(require_admin)):
        with db(request) as conn:
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not order:
                raise HTTPException(404, 'Pedido não encontrado.')
            if order['status'] == body.status:
                return {'order': order_dict(order)}
            if body.status not in TRANSITIONS.get(order['status'], set()):
                raise HTTPException(409, 'Essa mudança de etapa não é permitida.')
            if body.status == 'cancelled' and (order['payment_status'] == 'paid' or operations.has_receivable_payments(conn, order_id)):
                raise HTTPException(409, 'O pedido tem recebimento. O cancelamento com estorno ainda não está disponível no sistema.')
            if body.status in ('confirmed', 'preparing', 'ready') and not order['stock_applied']:
                operations.apply_stock(conn, order)
                marketing.record_coupon_use(conn, order)
                conn.execute('UPDATE orders SET stock_applied=1,confirmed_at=? WHERE id=?', (utcnow(), order_id))
            if body.status == 'cancelled' and order['status'] == 'confirmed':
                operations.release_stock(conn, order)
                conn.execute('UPDATE orders SET stock_applied=0 WHERE id=?', (order_id,))
            conn.execute('UPDATE orders SET status=?,delivered_at=CASE WHEN ?=\'delivered\' THEN ? ELSE delivered_at END WHERE id=?',
                         (body.status, body.status, utcnow(), order_id))
            conn.execute('INSERT INTO order_events VALUES(?,?,?,?)', (uuid.uuid4().hex, order_id, body.status, utcnow()))
            fresh = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            marketing.apply_loyalty(conn, fresh)
            whatsapp.order_notice(conn, fresh)
            return {'order': order_dict(conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())}

    @app.post('/api/admin/orders/{order_id}/payment')
    def payment(order_id: str, body: PaymentInput, request: Request, admin=Depends(require_admin)):
        with db(request) as conn:
            order = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            if not order or order['status'] == 'cancelled':
                raise HTTPException(409, 'Não é possível registrar pagamento para esse pedido.')
            conn.execute('UPDATE orders SET payment_status=\'paid\' WHERE id=?', (order_id,))
            fresh = conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone()
            operations.record_sale(conn, fresh)
            marketing.apply_loyalty(conn, fresh)
            return {'order': order_dict(conn.execute('SELECT * FROM orders WHERE id=?', (order_id,)).fetchone())}

    @app.post('/api/events', status_code=201)
    def analytics(body: EventInput, request: Request):
        with db(request) as conn:
            conn.execute('INSERT INTO analytics_events VALUES(?,?)', (body.name, utcnow()))
        return {'ok': True}

    @app.get('/api/admin/dashboard')
    def dashboard(request: Request, admin=Depends(require_admin)):
        with db(request) as conn:
            count = conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]
            sales, paid = conn.execute('SELECT COALESCE(SUM(total_cents),0),COUNT(*) FROM orders WHERE payment_status=\'paid\' AND status<>\'cancelled\'').fetchone()
            views = conn.execute('SELECT COUNT(*) FROM analytics_events WHERE name=\'view\'').fetchone()[0]
            web_orders = conn.execute('SELECT COUNT(*) FROM orders WHERE source=\'web\'').fetchone()[0]
            return {'orders_count': count, 'sales_cents': sales, 'ticket_cents': round(sales / paid) if paid else 0,
                    'by_status': [dict(row) for row in conn.execute('SELECT status,COUNT(*) AS count FROM orders GROUP BY status')],
                    'daily_sales': [dict(row) for row in conn.execute('SELECT date(created_at,\'-3 hours\') AS date,SUM(total_cents) AS total_cents FROM orders WHERE payment_status=\'paid\' AND status<>\'cancelled\' GROUP BY date ORDER BY date DESC LIMIT 30')],
                    'conversion_rate': web_orders / views if views else None, 'views': views}

    app.include_router(operations.router)
    app.include_router(marketing.router)
    app.include_router(whatsapp.router)

    @app.get('/system-config.js')
    def system_config():
        return Response('window.SaharaSystemConfig = Object.freeze({apiBase:"/api"});', media_type='application/javascript')

    @app.get('/{asset:path}')
    def static_asset(asset: str):
        asset = asset or 'index.html'
        public = {'index.html', 'app.js', 'address.js', 'ordering.js', 'ordering.css', 'style.css', 'pwa.js', 'pwa.css',
                  'service-worker.js', 'manifest.webmanifest', 'admin.html', 'admin.css', 'admin.js', 'esfihas.png', 'logo-sahara.jpg'}
        allowed_image = asset.startswith(('images/', 'icons/')) and Path(asset).suffix.lower() in ('.png', '.jpg', '.jpeg', '.svg', '.ico')
        if asset not in public and not allowed_image:
            raise HTTPException(404, 'Arquivo não encontrado.')
        target = (ROOT / asset).resolve()
        if ROOT not in target.parents or not target.is_file():
            raise HTTPException(404, 'Arquivo não encontrado.')
        return FileResponse(target)

    return app

app = create_app()
