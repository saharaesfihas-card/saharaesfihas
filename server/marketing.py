"""Customer records, coupons and consent-based marketing for Sahara.

Money is always an integer number of cents. Campaigns are drafts: this module
does not send WhatsApp messages or report delivery without an integration.
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from .core import db, require_admin, utcnow

router = APIRouter()
ADMIN = [Depends(require_admin)]


def initialize(conn: sqlite3.Connection) -> None:
    """Create additive tables; core owns customers, products and orders."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS coupons (
            id TEXT PRIMARY KEY,
            code TEXT NOT NULL UNIQUE COLLATE NOCASE,
            kind TEXT NOT NULL CHECK (kind IN ('percent', 'fixed')),
            value INTEGER NOT NULL CHECK (value > 0),
            min_subtotal_cents INTEGER NOT NULL DEFAULT 0 CHECK (min_subtotal_cents >= 0),
            max_discount_cents INTEGER CHECK (max_discount_cents > 0),
            usage_limit INTEGER CHECK (usage_limit > 0),
            per_customer_limit INTEGER CHECK (per_customer_limit > 0),
            uses INTEGER NOT NULL DEFAULT 0 CHECK (uses >= 0),
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS loyalty_rewards (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            points_cost INTEGER NOT NULL CHECK (points_cost > 0),
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS loyalty_redemptions (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL REFERENCES customers(id),
            reward_id TEXT NOT NULL REFERENCES loyalty_rewards(id),
            reward_name TEXT NOT NULL,
            points_cost INTEGER NOT NULL CHECK (points_cost > 0),
            idempotency_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS loyalty_ledger (
            id TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL REFERENCES customers(id),
            order_id TEXT UNIQUE REFERENCES orders(id),
            redemption_id TEXT UNIQUE REFERENCES loyalty_redemptions(id),
            points INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS campaigns (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('promotion', 'loyalty', 'recovery')),
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status = 'draft'),
            audience_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
            rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
            comment TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS marketing_orders_customer ON orders(customer_id);
        CREATE INDEX IF NOT EXISTS marketing_orders_coupon ON orders(coupon_code);
    """)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _phone(value: str) -> str:
    value = re.sub(r"\D", "", value)
    if not 10 <= len(value) <= 15:
        raise ValueError("Informe um telefone com DDD, com 10 a 15 dígitos.")
    return value


class CustomerCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    phone: str = Field(min_length=10, max_length=30)
    marketing_opt_in: bool = False

    _normalize_phone = field_validator("phone")(_phone)


class CustomerUpdate(Input):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, min_length=10, max_length=30)
    marketing_opt_in: bool | None = None

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str | None) -> str | None:
        return _phone(value) if value is not None else None


def _coupon_code(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{2,40}", value):
        raise ValueError("Cupom deve ter de 2 a 40 letras, números, hífens ou sublinhados.")
    return value


class CouponCreate(Input):
    code: str
    kind: Literal["percent", "fixed"]
    value: int = Field(gt=0, le=100_000_000, strict=True)
    min_subtotal_cents: int = Field(default=0, ge=0, le=1_000_000_000, strict=True)
    max_discount_cents: int | None = Field(default=None, gt=0, le=1_000_000_000, strict=True)
    usage_limit: int | None = Field(default=None, gt=0, le=1_000_000_000, strict=True,
                                   validation_alias=AliasChoices("usage_limit", "max_uses"))
    per_customer_limit: int | None = Field(default=None, gt=0, le=1_000_000_000, strict=True)
    expires_at: datetime | None = None
    active: bool = True

    _normalize_code = field_validator("code")(_coupon_code)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("A validade do cupom precisa incluir o fuso horário.")
            return value.astimezone(timezone.utc)
        return value


class CouponUpdate(Input):
    code: str | None = None
    kind: Literal["percent", "fixed"] | None = None
    value: int | None = Field(default=None, gt=0, le=100_000_000, strict=True)
    min_subtotal_cents: int | None = Field(default=None, ge=0, le=1_000_000_000, strict=True)
    max_discount_cents: int | None = Field(default=None, gt=0, le=1_000_000_000, strict=True)
    usage_limit: int | None = Field(default=None, gt=0, le=1_000_000_000, strict=True,
                                   validation_alias=AliasChoices("usage_limit", "max_uses"))
    per_customer_limit: int | None = Field(default=None, gt=0, le=1_000_000_000, strict=True)
    expires_at: datetime | None = None
    active: bool | None = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str | None) -> str | None:
        return _coupon_code(value) if value is not None else None

    _aware_expiry = field_validator("expires_at")(CouponCreate.aware_expiry.__func__)


class RewardCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    points_cost: int = Field(gt=0, le=10_000_000, strict=True)
    active: bool = True


class RedemptionCreate(Input):
    customer_id: str = Field(min_length=1, max_length=100)
    reward_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=120)


class CampaignCreate(Input):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["promotion", "loyalty", "recovery"] = "promotion"
    message: str = Field(min_length=1, max_length=4000)
    customer_ids: list[str] | None = Field(default=None, max_length=1000)


class ReviewCreate(Input):
    token: str = Field(min_length=1, max_length=200)
    rating: int = Field(ge=1, le=5, strict=True)
    comment: str = Field(default="", max_length=2000)


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("active", "marketing_opt_in"):
        if key in data:
            data[key] = bool(data[key])
    return data


def _find(conn: sqlite3.Connection, table: str, record_id: str) -> sqlite3.Row:
    # table names are exclusively fixed literals supplied inside this module.
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Registro não encontrado.")
    return row


def _update(conn: sqlite3.Connection, table: str, record_id: str, changes: dict[str, Any]) -> None:
    if not changes:
        raise HTTPException(422, "Informe pelo menos um campo para atualizar.")
    assignments = ", ".join(f"{key} = ?" for key in changes)
    values = list(changes.values()) + [record_id]
    conn.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", values)


def _date(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Legacy timestamps from existing local databases are interpreted as UTC.
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _validate_coupon_data(data: dict[str, Any]) -> None:
    if data["kind"] == "percent" and data["value"] > 100:
        raise HTTPException(422, "O desconto percentual deve ser de 1 a 100.")


def _limited_coupon(conn: sqlite3.Connection, code: str,
                    customer_id: str | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM coupons WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
    if row is None or not row["active"]:
        raise HTTPException(400, "Cupom inválido ou inativo.")
    if row["expires_at"] and _date(row["expires_at"]) <= datetime.now(timezone.utc):
        raise HTTPException(400, "Este cupom expirou.")
    if row["usage_limit"] is not None and row["uses"] >= row["usage_limit"]:
        raise HTTPException(400, "O limite de utilização deste cupom foi atingido.")
    if row["per_customer_limit"] is not None:
        if not customer_id:
            raise HTTPException(400, "Identifique o cliente para utilizar este cupom.")
        count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE customer_id = ? AND coupon_code = ? "
            "COLLATE NOCASE AND coupon_applied = 1", (customer_id, row["code"])
        ).fetchone()[0]
        if count >= row["per_customer_limit"]:
            raise HTTPException(400, "O limite deste cupom para o cliente foi atingido.")
    return row


def coupon_discount(conn: sqlite3.Connection, code: str | None, subtotal_cents: int,
                    customer_id: str | None = None) -> int:
    """Compute discounts exclusively from server-side rules, without consuming use."""
    if not code or not code.strip():
        return 0
    if type(subtotal_cents) is not int or subtotal_cents < 0:
        raise HTTPException(422, "Subtotal inválido.")
    coupon = _limited_coupon(conn, code.strip().upper(), customer_id)
    if subtotal_cents < coupon["min_subtotal_cents"]:
        raise HTTPException(400, "O pedido não atingiu o valor mínimo deste cupom.")
    discount = (subtotal_cents * coupon["value"] // 100
                if coupon["kind"] == "percent" else coupon["value"])
    if coupon["max_discount_cents"] is not None:
        discount = min(discount, coupon["max_discount_cents"])
    return min(subtotal_cents, discount)


def record_coupon_use(conn: sqlite3.Connection, order: sqlite3.Row | dict[str, Any]) -> bool:
    """Claim one use during order confirmation, in the caller's transaction.

    Historical uses stay consumed after cancellation. Raising rolls back the
    order transition and its claim through core.db's transaction context.
    """
    if not order["coupon_code"]:
        return False
    claimed = conn.execute(
        "UPDATE orders SET coupon_applied = 1 WHERE id = ? AND coupon_applied = 0",
        (order["id"],),
    ).rowcount
    if not claimed:
        return False
    # The order claim above obtains SQLite's write lock before checking limits.
    coupon = conn.execute("SELECT * FROM coupons WHERE code = ? COLLATE NOCASE",
                          (order["coupon_code"],)).fetchone()
    if coupon is None:
        raise HTTPException(409, "O cupom do pedido não está mais disponível.")
    if coupon["per_customer_limit"] is not None:
        used = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE customer_id = ? AND coupon_code = ? "
            "COLLATE NOCASE AND coupon_applied = 1 AND id != ?",
            (order["customer_id"], coupon["code"], order["id"]),
        ).fetchone()[0]
        if not order["customer_id"] or used >= coupon["per_customer_limit"]:
            raise HTTPException(409, "O limite deste cupom para o cliente foi atingido.")
    changed = conn.execute(
        "UPDATE coupons SET uses = uses + 1 WHERE id = ? "
        "AND (usage_limit IS NULL OR uses < usage_limit)", (coupon["id"],)
    ).rowcount
    if not changed:
        raise HTTPException(409, "O limite de utilização deste cupom foi atingido.")
    return True


def apply_loyalty(conn: sqlite3.Connection, order: sqlite3.Row | dict[str, Any]) -> int:
    """Credit floor(net order cents / 100) once, only for delivered+paid orders."""
    fresh = conn.execute("SELECT * FROM orders WHERE id = ?", (order["id"],)).fetchone()
    if (fresh is None or fresh["status"] != "delivered" or fresh["payment_status"] != "paid"
            or not fresh["customer_id"] or fresh["loyalty_applied"]):
        return 0
    if conn.execute("SELECT 1 FROM customers WHERE id = ?", (fresh["customer_id"],)).fetchone() is None:
        return 0
    claimed = conn.execute(
        "UPDATE orders SET loyalty_applied = 1 WHERE id = ? AND loyalty_applied = 0 "
        "AND status = 'delivered' AND payment_status = 'paid'", (fresh["id"],)
    ).rowcount
    if not claimed:
        return 0
    points = max(0, fresh["total_cents"] // 100)
    conn.execute("UPDATE customers SET points = points + ? WHERE id = ?",
                 (points, fresh["customer_id"]))
    conn.execute(
        "INSERT INTO loyalty_ledger (id, customer_id, order_id, points, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(uuid4()), fresh["customer_id"], fresh["id"], points, utcnow()),
    )
    return points


@router.get("/api/admin/customers", dependencies=ADMIN)
def customers(request: Request, q: str = "") -> dict[str, Any]:
    q = q.strip()[:120]
    with db(request) as conn:
        records = conn.execute(
            "SELECT * FROM customers WHERE name LIKE ? OR phone LIKE ? ORDER BY name LIMIT 1000",
            (f"%{q}%", f"%{re.sub(r'[^0-9]', '', q) or q}%"),
        ).fetchall()
    return {"customers": [_dict(row) for row in records]}


@router.post("/api/admin/customers", dependencies=ADMIN, status_code=201)
def create_customer(body: CustomerCreate, request: Request) -> dict[str, Any]:
    record_id = str(uuid4())
    with db(request) as conn:
        try:
            conn.execute(
                "INSERT INTO customers (id, name, phone, marketing_opt_in, created_at, points) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (record_id, body.name, body.phone, int(body.marketing_opt_in), utcnow()),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Este telefone já está cadastrado.") from exc
        result = _dict(_find(conn, "customers", record_id))
    return {"customer": result}


@router.patch("/api/admin/customers/{customer_id}", dependencies=ADMIN)
def update_customer(customer_id: str, body: CustomerUpdate, request: Request) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    if any(value is None for value in changes.values()):
        raise HTTPException(422, "Os dados do cliente não podem ser nulos.")
    with db(request) as conn:
        _find(conn, "customers", customer_id)
        try:
            _update(conn, "customers", customer_id, changes)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Este telefone já está cadastrado.") from exc
        result = _dict(_find(conn, "customers", customer_id))
    return {"customer": result}


@router.get("/api/admin/coupons", dependencies=ADMIN)
def coupons(request: Request) -> dict[str, Any]:
    with db(request) as conn:
        records = conn.execute("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 1000").fetchall()
    return {"coupons": [_dict(row) for row in records]}


@router.post("/api/admin/coupons", dependencies=ADMIN, status_code=201)
def create_coupon(body: CouponCreate, request: Request) -> dict[str, Any]:
    data = body.model_dump()
    _validate_coupon_data(data)
    data.update(id=str(uuid4()), created_at=utcnow())
    data["expires_at"] = body.expires_at.isoformat() if body.expires_at else None
    columns = list(data)
    with db(request) as conn:
        try:
            conn.execute(f"INSERT INTO coupons ({', '.join(columns)}) VALUES "
                         f"({', '.join('?' for _ in columns)})", list(data.values()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Este código de cupom já existe.") from exc
        result = _dict(_find(conn, "coupons", data["id"]))
    return {"coupon": result}


@router.patch("/api/admin/coupons/{coupon_id}", dependencies=ADMIN)
def update_coupon(coupon_id: str, body: CouponUpdate, request: Request) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    required = ("code", "kind", "value", "min_subtotal_cents", "active")
    if any(key in changes and changes[key] is None for key in required):
        raise HTTPException(422, "Os campos obrigatórios do cupom não podem ser nulos.")
    if isinstance(changes.get("expires_at"), datetime):
        changes["expires_at"] = changes["expires_at"].isoformat()
    with db(request) as conn:
        old = _dict(_find(conn, "coupons", coupon_id))
        _validate_coupon_data(old | changes)
        try:
            _update(conn, "coupons", coupon_id, changes)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Este código de cupom já existe.") from exc
        result = _dict(_find(conn, "coupons", coupon_id))
    return {"coupon": result}


@router.get("/api/admin/rfv", dependencies=ADMIN)
def rfv(request: Request) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    with db(request) as conn:
        rows = conn.execute(
            "SELECT c.*, COUNT(o.id) AS frequency, COALESCE(SUM(o.total_cents), 0) AS monetary_cents, "
            "MAX(COALESCE(o.delivered_at, o.created_at)) AS last_order_at "
            "FROM customers c LEFT JOIN orders o ON o.customer_id = c.id "
            "AND o.status = 'delivered' AND o.payment_status = 'paid' "
            "GROUP BY c.id ORDER BY monetary_cents DESC, c.name",
        ).fetchall()
    results = []
    for row in rows:
        data = _dict(row)
        recency = max(0, (now - _date(data["last_order_at"])).days) if data["last_order_at"] else None
        frequency = data["frequency"]
        if not frequency:
            segment = "sem_compras"
        elif recency <= 30:
            segment = "vip" if frequency >= 5 else "recorrente" if frequency >= 2 else "novo"
        else:
            segment = "em_risco" if recency <= 60 else "inativo"
        data.update(recency_days=recency, segment=segment)
        results.append(data)
    return {
        "customers": results,
        "rules": {
            "eligible_orders": "delivered+paid",
            "recency": "Dias inteiros desde a última entrega paga; sem compras = null.",
            "frequency": "Quantidade de pedidos entregues e pagos.",
            "monetary": "Soma dos totais líquidos, em centavos, de pedidos entregues e pagos.",
            "segments": {"vip": "Até 30 dias e pelo menos 5 pedidos", "recorrente": "Até 30 dias e 2 a 4 pedidos",
                         "novo": "Até 30 dias e 1 pedido", "em_risco": "31 a 60 dias",
                         "inativo": "Mais de 60 dias", "sem_compras": "Nenhum pedido entregue e pago"},
        },
    }


@router.get("/api/admin/loyalty/rewards", dependencies=ADMIN)
def rewards(request: Request) -> dict[str, Any]:
    with db(request) as conn:
        rows = conn.execute("SELECT * FROM loyalty_rewards ORDER BY points_cost, name LIMIT 1000").fetchall()
    return {"rewards": [_dict(row) for row in rows], "points_rule": "1 ponto por R$ 1 do total líquido de cada pedido entregue e pago, arredondado para baixo."}


@router.post("/api/admin/loyalty/rewards", dependencies=ADMIN, status_code=201)
def create_reward(body: RewardCreate, request: Request) -> dict[str, Any]:
    reward_id = str(uuid4())
    with db(request) as conn:
        conn.execute(
            "INSERT INTO loyalty_rewards (id, name, description, points_cost, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (reward_id, body.name, body.description, body.points_cost, int(body.active), utcnow()),
        )
        result = _dict(_find(conn, "loyalty_rewards", reward_id))
    return {"reward": result}


@router.post("/api/admin/loyalty/redeem", dependencies=ADMIN)
def redeem_reward(body: RedemptionCreate, request: Request) -> dict[str, Any]:
    with db(request) as conn:
        replay = conn.execute("SELECT * FROM loyalty_redemptions WHERE idempotency_key = ?",
                              (body.idempotency_key,)).fetchone()
        if replay:
            if replay["customer_id"] != body.customer_id or replay["reward_id"] != body.reward_id:
                raise HTTPException(409, "Esta chave de resgate já pertence a outra operação.")
            balance = _find(conn, "customers", body.customer_id)["points"]
            return {"redemption": dict(replay), "remaining_points": balance, "replayed": True}
        _find(conn, "customers", body.customer_id)
        reward = _find(conn, "loyalty_rewards", body.reward_id)
        if not reward["active"]:
            raise HTTPException(400, "Esta recompensa está inativa.")
        changed = conn.execute("UPDATE customers SET points = points - ? WHERE id = ? AND points >= ?",
                               (reward["points_cost"], body.customer_id, reward["points_cost"])).rowcount
        if not changed:
            raise HTTPException(400, "O cliente não possui pontos suficientes.")
        redemption_id, created_at = str(uuid4()), utcnow()
        conn.execute(
            "INSERT INTO loyalty_redemptions (id, customer_id, reward_id, reward_name, points_cost, idempotency_key, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (redemption_id, body.customer_id, body.reward_id, reward["name"], reward["points_cost"], body.idempotency_key, created_at),
        )
        conn.execute("INSERT INTO loyalty_ledger (id, customer_id, redemption_id, points, created_at) VALUES (?, ?, ?, ?, ?)",
                     (str(uuid4()), body.customer_id, redemption_id, -reward["points_cost"], created_at))
        result = dict(_find(conn, "loyalty_redemptions", redemption_id))
        balance = _find(conn, "customers", body.customer_id)["points"]
    return {"redemption": result, "remaining_points": balance, "replayed": False}


def _campaign(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    audience = json.loads(data.pop("audience_json"))
    if audience:
        placeholders = ",".join("?" for _ in audience)
        allowed = conn.execute(f"SELECT id FROM customers WHERE marketing_opt_in = 1 AND id IN ({placeholders})",
                               audience).fetchall()
        data["customer_ids"] = [customer["id"] for customer in allowed]
    else:
        data["customer_ids"] = []
    data["audience_count"] = len(data["customer_ids"])
    data["sending_available"] = False
    return data


@router.get("/api/admin/campaigns", dependencies=ADMIN)
def campaigns(request: Request) -> dict[str, Any]:
    with db(request) as conn:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 1000").fetchall()
        results = [_campaign(conn, row) for row in rows]
    return {"campaigns": results, "sending_available": False}


@router.post("/api/admin/campaigns", dependencies=ADMIN, status_code=201)
def create_campaign(body: CampaignCreate, request: Request) -> dict[str, Any]:
    with db(request) as conn:
        if body.customer_ids is None:
            ids = [row["id"] for row in conn.execute("SELECT id FROM customers WHERE marketing_opt_in = 1 ORDER BY id")]
        else:
            ids = list(dict.fromkeys(body.customer_ids))
            for customer_id in ids:
                customer = _find(conn, "customers", customer_id)
                if not customer["marketing_opt_in"]:
                    raise HTTPException(400, "O público deve conter apenas clientes com consentimento de marketing.")
        if not ids:
            raise HTTPException(400, "Nenhum cliente com consentimento foi selecionado.")
        campaign_id = str(uuid4())
        conn.execute("INSERT INTO campaigns (id, name, kind, message, status, audience_json, created_at) VALUES (?, ?, ?, ?, 'draft', ?, ?)",
                     (campaign_id, body.name, body.kind, body.message, json.dumps(ids), utcnow()))
        result = _campaign(conn, _find(conn, "campaigns", campaign_id))
    return {"campaign": result, "sending_available": False}


@router.get("/api/admin/reviews", dependencies=ADMIN)
def reviews(request: Request) -> dict[str, Any]:
    with db(request) as conn:
        rows = conn.execute("SELECT * FROM reviews ORDER BY created_at DESC LIMIT 1000").fetchall()
    return {"reviews": [dict(row) for row in rows]}


@router.post("/api/orders/{order_id}/review", status_code=201)
def create_review(order_id: str, body: ReviewCreate, request: Request) -> dict[str, Any]:
    with db(request) as conn:
        order = conn.execute("SELECT id, status, tracking_token FROM orders WHERE id = ?", (order_id,)).fetchone()
        expected = str(order["tracking_token"]) if order else ""
        if not order or not hmac.compare_digest(expected.encode(), body.token.encode()):
            raise HTTPException(404, "Pedido não encontrado.")
        if order["status"] != "delivered":
            raise HTTPException(409, "A avaliação fica disponível após a entrega.")
        review_id = str(uuid4())
        try:
            conn.execute("INSERT INTO reviews (id, order_id, rating, comment, created_at) VALUES (?, ?, ?, ?, ?)",
                         (review_id, order_id, body.rating, body.comment, utcnow()))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Este pedido já possui uma avaliação.") from exc
    # Public acknowledgement carries no customer, order, contact or tracking data.
    return {"review": {"id": review_id, "rating": body.rating}, "message": "Obrigado pela sua avaliação."}
