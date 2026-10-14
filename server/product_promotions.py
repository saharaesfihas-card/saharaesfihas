"""Preços semanais compartilhados pelo site, PDV e atendimento; começam pausados."""
from datetime import datetime
from zoneinfo import ZoneInfo

DAYS = ('segunda-feira', 'terça-feira', 'quarta-feira', 'quinta-feira',
        'sexta-feira', 'sábado', 'domingo')
DEFAULTS = (('calabresa', 'Segunda da Calabresa', 0),
            ('frango', 'Terça do Frango', 1), ('carne', 'Quarta da Carne', 2))


def local_now():
    return datetime.now(ZoneInfo('America/Sao_Paulo'))


def initialize(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS product_promotions (
        product_id TEXT PRIMARY KEY REFERENCES products(id), name TEXT NOT NULL,
        weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
        price_cents INTEGER NOT NULL CHECK(price_cents>0),
        enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)))''')
    for identifier, name, weekday in DEFAULTS:
        # Deploys never reactivate a paused campaign or reset the owner's choice.
        conn.execute('''INSERT OR IGNORE INTO product_promotions
            (product_id,name,weekday,price_cents,enabled) VALUES(?,?,?,299,0)''',
                     (identifier, name, weekday))


def apply_price(conn, product, now=None):
    result = dict(product)
    if 'available' in result:
        result['available'] = bool(result['available'])
    promotion = conn.execute('SELECT * FROM product_promotions WHERE product_id=?',
                             (result['id'],)).fetchone()
    if (promotion and promotion['enabled'] and result.get('available', True)
            and (now or local_now()).weekday() == promotion['weekday']):
        result['regular_price_cents'] = result['price_cents']
        result['price_cents'] = promotion['price_cents']
        result['promotion'] = {'name': promotion['name'], 'day': DAYS[promotion['weekday']]}
    return result


def catalog(conn, now=None):
    moment = now or local_now()
    return [apply_price(conn, row, moment) for row in conn.execute(
        'SELECT * FROM products WHERE available=1 ORDER BY category,name')]


def product(conn, identifier, now=None):
    row = conn.execute('SELECT * FROM products WHERE id=?', (identifier,)).fetchone()
    return apply_price(conn, row, now) if row else None


def dashboard(conn):
    moment = local_now()
    result = []
    for row in conn.execute('''SELECT pp.*,p.name AS product_name,
            p.price_cents AS regular_price_cents,p.available
            FROM product_promotions pp JOIN products p ON p.id=pp.product_id
            ORDER BY pp.weekday'''):
        item = dict(row)
        item['enabled'] = bool(item['enabled'])
        item['available'] = bool(item['available'])
        item['day'] = DAYS[item['weekday']]
        item['effective_today'] = (item['enabled'] and item['available']
                                   and item['weekday'] == moment.weekday())
        result.append(item)
    return {'promotions': result, 'timezone': 'America/Sao_Paulo',
            'local_date': moment.date().isoformat()}
