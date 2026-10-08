"""Persistência privada, autenticação e modelos compartilhados."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT.parent / '.sahara-system-data' / 'sahara.sqlite3'
COOKIE = 'sahara_session'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS products (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price_cents INTEGER NOT NULL CHECK(price_cents>=0)
);
CREATE TABLE IF NOT EXISTS customers (
 id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', phone TEXT UNIQUE,
 marketing_opt_in INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, points INTEGER NOT NULL DEFAULT 0 CHECK(points>=0)
);
CREATE TABLE IF NOT EXISTS orders (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, requested_for TEXT, customer_id TEXT REFERENCES customers(id),
 customer_name TEXT NOT NULL DEFAULT '', customer_phone TEXT NOT NULL DEFAULT '', delivery_json TEXT NOT NULL,
 items_json TEXT NOT NULL, subtotal_cents INTEGER NOT NULL, discount_cents INTEGER NOT NULL DEFAULT 0,
 total_cents INTEGER NOT NULL CHECK(total_cents>=0), coupon_code TEXT, status TEXT NOT NULL DEFAULT 'preparing',
 payment_method TEXT NOT NULL DEFAULT '', payment_status TEXT NOT NULL DEFAULT 'unpaid', tracking_token TEXT NOT NULL,
 idempotency_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL, stock_applied INTEGER NOT NULL DEFAULT 0,
 loyalty_applied INTEGER NOT NULL DEFAULT 0, coupon_applied INTEGER NOT NULL DEFAULT 0, courier_id TEXT,
 source TEXT NOT NULL DEFAULT 'web', notes TEXT NOT NULL DEFAULT '', confirmed_at TEXT, delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (actor TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_events (id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id), status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_events (name TEXT NOT NULL, created_at TEXT NOT NULL);
'''

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def hash_password(password):
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError('Use uma senha com pelo menos oito caracteres.')
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return 'scrypt$' + salt.hex() + '$' + digest.hex()

def verify_password(password, encoded):
    try:
        kind, salt, expected = encoded.split('$')
        if kind != 'scrypt' or len(password) > 1024:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (AttributeError, ValueError, TypeError):
        return False

@contextmanager
def db(request=None):
    path = request.app.state.db_path if request is not None else DEFAULT_DB
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    try:
        conn.execute('BEGIN IMMEDIATE')
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

def require_admin(request: Request):
    token = request.cookies.get(COOKIE, '')
    if not token:
        raise HTTPException(401, 'Entre no painel da loja.')
    with db(request) as conn:
        session = conn.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?',
                               (hashlib.sha256(token.encode()).hexdigest(), int(time.time()))).fetchone()
    if not session:
        raise HTTPException(401, 'Sua sessão expirou. Entre novamente.')
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            raise HTTPException(403, 'Origem não autorizada para alterar a gestão.')
        if not hmac.compare_digest(request.headers.get('x-sahara-csrf', '').encode('utf-8'), session['csrf'].encode('utf-8')):
            raise HTTPException(403, 'Atualize o painel e tente novamente.')
    return dict(session)

def order_dict(row):
    result = dict(row)
    result['items'] = json.loads(result.pop('items_json'))
    result['delivery'] = json.loads(result.pop('delivery_json'))
    for key in ('tracking_token', 'request_hash', 'idempotency_key'):
        result.pop(key, None)
    return result
