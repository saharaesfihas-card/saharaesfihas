"""Operação de delivery: estoque, caixa, fiado e atribuição de entregas.

Todos os valores financeiros são centavos inteiros. As mutações de estoque
participam da mesma transação do pedido, fornecida por ``core.db``.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .core import db, order_dict, require_admin, utcnow


router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])
PositiveCents = Annotated[StrictInt, Field(gt=0, le=1_000_000_000)]
NonnegativeCents = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
Quantity = Annotated[Decimal, Field(ge=0, le=1_000_000_000, allow_inf_nan=False, decimal_places=6)]
PositiveQuantity = Annotated[Decimal, Field(gt=0, le=1_000_000_000, allow_inf_nan=False, decimal_places=6)]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InventoryInput(Input):
    id: Identifier | None = None
    label: str = Field(min_length=1, max_length=100)
    unit: Literal["un", "g", "kg", "ml", "l"] = "un"
    on_hand: Quantity = Decimal("0")
    low_threshold: Quantity = Decimal("0")
    product_id: Identifier | None = None


class AdjustmentInput(Input):
    quantity: Annotated[Decimal, Field(ge=-1_000_000_000, le=1_000_000_000, allow_inf_nan=False, decimal_places=6)]
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("quantity")
    @classmethod
    def nonzero(cls, value):
        if value == 0:
            raise ValueError("A quantidade deve ser diferente de zero.")
        return value


class RecipeComponent(Input):
    inventory_id: Identifier
    quantity: PositiveQuantity


class RecipeInput(Input):
    product_id: Identifier
    components: list[RecipeComponent] = Field(max_length=100)

    @field_validator("components")
    @classmethod
    def unique_components(cls, value):
        if len({item.inventory_id for item in value}) != len(value):
            raise ValueError("Um ingrediente não pode aparecer duas vezes na receita.")
        return value


class SessionInput(Input):
    action: Literal["open", "close"]
    session_id: Identifier | None = None
    opening_cents: NonnegativeCents = 0
    closing_cents: NonnegativeCents | None = None
    notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def close_requires_amount(self):
        if self.action == "close" and self.closing_cents is None:
            raise ValueError("Informe o valor contado para fechar o caixa.")
        if self.action == "open" and (self.session_id or self.closing_cents is not None):
            raise ValueError("Não informe sessão ou valor de fechamento ao abrir o caixa.")
        return self


class CashEntryInput(Input):
    kind: Literal["credit", "debit", "supply", "withdrawal"]
    amount_cents: PositiveCents
    description: str = Field(min_length=1, max_length=500)
    session_id: Identifier | None = None


class ReceivableInput(Input):
    customer_name: str = Field(min_length=1, max_length=100)
    phone: str = Field(default="", max_length=30)
    amount_cents: PositiveCents
    description: str = Field(default="", max_length=500)
    due_date: date | None = None
    order_id: Identifier | None = None


class ReceivablePaymentInput(Input):
    amount_cents: PositiveCents
    payment_method: Literal["dinheiro", "pix", "cartao", "outro"] = "dinheiro"
    description: str = Field(default="", max_length=500)
    idempotency_key: Annotated[str, Field(min_length=16, max_length=128)]


class DriverInput(Input):
    id: Identifier | None = None
    name: str = Field(min_length=1, max_length=100)
    phone: str = Field(default="", max_length=30)
    active: bool = True


class DriverAssignment(Input):
    driver_id: Identifier | None = None


class PrinterInput(Input):
    id: Identifier | None = None
    name: str = Field(min_length=1, max_length=100)
    paper_width: Literal[58, 80] = 80
    copies: Annotated[StrictInt, Field(ge=1, le=5)] = 1
    active: bool = True


def initialize(conn):
    """Cria apenas as tabelas operacionais, sem dados demonstrativos."""
    statements = [
        """CREATE TABLE IF NOT EXISTS inventory (
            id TEXT PRIMARY KEY, label TEXT NOT NULL, unit TEXT NOT NULL,
            on_hand REAL NOT NULL CHECK(on_hand >= 0),
            low_threshold REAL NOT NULL DEFAULT 0 CHECK(low_threshold >= 0),
            product_id TEXT UNIQUE REFERENCES products(id), updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS inventory_movements (
            id TEXT PRIMARY KEY, inventory_id TEXT NOT NULL REFERENCES inventory(id),
            quantity REAL NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
            order_id TEXT REFERENCES orders(id), kind TEXT NOT NULL,
            UNIQUE(order_id, inventory_id, kind)
        )""",
        """CREATE TABLE IF NOT EXISTS recipes (
            product_id TEXT NOT NULL REFERENCES products(id),
            inventory_id TEXT NOT NULL REFERENCES inventory(id),
            quantity REAL NOT NULL CHECK(quantity > 0),
            PRIMARY KEY(product_id, inventory_id)
        )""",
        """CREATE TABLE IF NOT EXISTS cash_sessions (
            id TEXT PRIMARY KEY, opened_at TEXT NOT NULL, closed_at TEXT,
            opening_cents INTEGER NOT NULL CHECK(opening_cents >= 0),
            closing_cents INTEGER CHECK(closing_cents >= 0),
            expected_cents INTEGER, notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('open','closed'))
        )""",
        """CREATE UNIQUE INDEX IF NOT EXISTS one_open_cash_session
            ON cash_sessions(status) WHERE status = 'open'""",
        """CREATE TABLE IF NOT EXISTS cash_entries (
            id TEXT PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            description TEXT NOT NULL,
            session_id TEXT REFERENCES cash_sessions(id),
            order_id TEXT UNIQUE REFERENCES orders(id),
            receivable_payment_id TEXT REFERENCES receivable_payments(id)
        )""",
        """CREATE TABLE IF NOT EXISTS receivables (
            id TEXT PRIMARY KEY, customer_name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            description TEXT NOT NULL DEFAULT '', due_date TEXT,
            order_id TEXT UNIQUE REFERENCES orders(id), created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS receivable_payments (
            id TEXT PRIMARY KEY, receivable_id TEXT NOT NULL REFERENCES receivables(id),
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            payment_method TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, idempotency_key TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS drivers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS printer_profiles (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, paper_width INTEGER NOT NULL,
            copies INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )""",
    ]
    for statement in statements:
        conn.execute(statement)
    # Preserva bases inicializadas por versões anteriores destas instruções.
    if "receivable_payment_id" not in {row[1] for row in conn.execute("PRAGMA table_info(cash_entries)")}:
        conn.execute("ALTER TABLE cash_entries ADD COLUMN receivable_payment_id TEXT REFERENCES receivable_payments(id)")
    if "idempotency_key" not in {row[1] for row in conn.execute("PRAGMA table_info(receivable_payments)")}:
        conn.execute("ALTER TABLE receivable_payments ADD COLUMN idempotency_key TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS cash_receivable_receipt ON cash_entries(receivable_payment_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS receivable_payment_retries ON receivable_payments(idempotency_key)")


def _id():
    return str(uuid.uuid4())


def _found(conn, table, identifier, message):
    # table is always a literal controlled by this module, never request input.
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, message)
    return row


def _decimal(value):
    return Decimal(str(value))


def _inventory(conn):
    items = []
    for row in conn.execute("SELECT * FROM inventory ORDER BY label COLLATE NOCASE"):
        item = dict(row)
        item["low_stock"] = item["on_hand"] <= item["low_threshold"]
        items.append(item)
    return {"items": items}


def _movement(conn, inventory_id, quantity, reason, *, order_id=None, kind="adjustment"):
    conn.execute(
        "INSERT INTO inventory_movements VALUES (?,?,?,?,?,?,?)",
        (_id(), inventory_id, float(quantity), reason, utcnow(), order_id, kind),
    )


@router.get("/inventory")
def get_inventory(request: Request):
    with db(request) as conn:
        return _inventory(conn)


@router.post("/inventory")
def save_inventory(payload: InventoryInput, request: Request):
    with db(request) as conn:
        if payload.product_id:
            _found(conn, "products", payload.product_id, "Produto não encontrado.")
            duplicate = conn.execute(
                "SELECT id FROM inventory WHERE product_id = ? AND id != ?",
                (payload.product_id, payload.id or ""),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, "Este produto já possui um controle de estoque.")
        identifier = payload.id or _id()
        if payload.id:
            previous = _found(conn, "inventory", identifier, "Item de estoque não encontrado.")
            if payload.unit != previous["unit"]:
                raise HTTPException(409, "A unidade do estoque não pode ser alterada. Cadastre outro item.")
            if payload.product_id != previous["product_id"]:
                raise HTTPException(409, "O produto vinculado não pode ser alterado. Cadastre outro item.")
            quantity = payload.on_hand - _decimal(previous["on_hand"])
            conn.execute(
                "UPDATE inventory SET label=?, low_threshold=?, on_hand=?, updated_at=? WHERE id=?",
                (payload.label, float(payload.low_threshold), float(payload.on_hand), utcnow(), identifier),
            )
            if quantity:
                _movement(conn, identifier, quantity, "Saldo ajustado no cadastro do estoque.")
        else:
            conn.execute(
                "INSERT INTO inventory VALUES (?,?,?,?,?,?,?)",
                (identifier, payload.label, payload.unit, float(payload.on_hand),
                 float(payload.low_threshold), payload.product_id, utcnow()),
            )
            if payload.on_hand:
                _movement(conn, identifier, payload.on_hand, "Saldo inicial informado.")
        return _inventory(conn)


@router.post("/inventory/{inventory_id}/adjust")
def adjust_inventory(inventory_id: str, payload: AdjustmentInput, request: Request):
    with db(request) as conn:
        row = _found(conn, "inventory", inventory_id, "Item de estoque não encontrado.")
        balance = _decimal(row["on_hand"]) + payload.quantity
        if balance < 0:
            raise HTTPException(409, "A movimentação deixaria o estoque negativo.")
        if balance > Decimal("1000000000"):
            raise HTTPException(422, "Saldo de estoque acima do limite permitido.")
        conn.execute("UPDATE inventory SET on_hand=?,updated_at=? WHERE id=?",
                     (float(balance), utcnow(), inventory_id))
        _movement(conn, inventory_id, payload.quantity, payload.reason)
        return _inventory(conn)


def _recipes(conn):
    recipes = {}
    for row in conn.execute(
        """SELECT r.*,i.label,i.unit FROM recipes r JOIN inventory i ON i.id=r.inventory_id
           ORDER BY r.product_id,i.label COLLATE NOCASE"""
    ):
        recipe = recipes.setdefault(row["product_id"], {"product_id": row["product_id"], "components": []})
        recipe["components"].append({key: row[key] for key in ("inventory_id", "quantity", "label", "unit")})
    return {"recipes": list(recipes.values())}


@router.get("/recipes")
def get_recipes(request: Request):
    with db(request) as conn:
        return _recipes(conn)


@router.post("/recipes")
def save_recipe(payload: RecipeInput, request: Request):
    with db(request) as conn:
        _found(conn, "products", payload.product_id, "Produto não encontrado.")
        for component in payload.components:
            _found(conn, "inventory", component.inventory_id, "Ingrediente não encontrado.")
        conn.execute("DELETE FROM recipes WHERE product_id=?", (payload.product_id,))
        conn.executemany("INSERT INTO recipes VALUES (?,?,?)", [
            (payload.product_id, component.inventory_id, float(component.quantity))
            for component in payload.components
        ])
        return _recipes(conn)


def apply_stock(conn, order):
    """Consome ingredientes (ou estoque simples) uma única vez por pedido.

    Receitas têm prioridade sobre o estoque direto do mesmo produto. Produtos
    sem controle cadastrado não criam estoque ou deduções artificiais.
    """
    order = dict(order)
    if order.get("stock_applied") or conn.execute(
        "SELECT 1 FROM inventory_movements WHERE order_id=? AND kind='order' LIMIT 1", (order["id"],)
    ).fetchone():
        return False
    items = order.get("items")
    if items is None:
        items = json.loads(order["items_json"])
    required = defaultdict(Decimal)
    for item in items:
        quantity = item["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise HTTPException(422, "Quantidade inválida no pedido.")
        components = conn.execute("SELECT * FROM recipes WHERE product_id=?", (item["id"],)).fetchall()
        if components:
            for component in components:
                required[component["inventory_id"]] += _decimal(component["quantity"]) * quantity
        else:
            direct = conn.execute("SELECT id FROM inventory WHERE product_id=?", (item["id"],)).fetchone()
            if direct:
                required[direct["id"]] += Decimal(quantity)
    balances = {}
    for identifier, quantity in required.items():
        row = _found(conn, "inventory", identifier, "Ingrediente não encontrado.")
        balance = _decimal(row["on_hand"]) - quantity
        if balance < 0:
            raise HTTPException(409, f"Estoque insuficiente: {row['label']}.")
        balances[identifier] = balance
    for identifier, balance in balances.items():
        conn.execute("UPDATE inventory SET on_hand=?,updated_at=? WHERE id=?", (float(balance), utcnow(), identifier))
        _movement(conn, identifier, -required[identifier], "Consumo do pedido.", order_id=order["id"], kind="order")
    conn.execute("UPDATE orders SET stock_applied=1 WHERE id=?", (order["id"],))
    return True


def release_stock(conn, order):
    """Estorna o consumo original sem depender da receita atual e sem duplicar."""
    order = dict(order)
    movements = conn.execute(
        """SELECT m.* FROM inventory_movements m WHERE m.order_id=? AND m.kind='order'
           AND NOT EXISTS (SELECT 1 FROM inventory_movements r WHERE r.order_id=m.order_id
                           AND r.inventory_id=m.inventory_id AND r.kind='release')""", (order["id"],)
    ).fetchall()
    for movement in movements:
        row = _found(conn, "inventory", movement["inventory_id"], "Item de estoque não encontrado.")
        quantity = -_decimal(movement["quantity"])
        balance = _decimal(row["on_hand"]) + quantity
        conn.execute("UPDATE inventory SET on_hand=?,updated_at=? WHERE id=?",
                     (float(balance), utcnow(), movement["inventory_id"]))
        _movement(conn, movement["inventory_id"], quantity, "Estorno de pedido cancelado.",
                  order_id=order["id"], kind="release")
    conn.execute("UPDATE orders SET stock_applied=0 WHERE id=?", (order["id"],))
    return bool(movements)


def _session_balance(conn, session_id):
    return conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN kind IN ('credit','supply') THEN amount_cents
                                  ELSE -amount_cents END),0) FROM cash_entries WHERE session_id=?""",
        (session_id,),
    ).fetchone()[0]


def _sessions(conn):
    sessions = []
    active = None
    for row in conn.execute("SELECT * FROM cash_sessions ORDER BY opened_at DESC,id DESC"):
        item = dict(row)
        item["balance_cents"] = item["opening_cents"] + _session_balance(conn, item["id"])
        if item["status"] == "open":
            active = item
        sessions.append(item)
    return {"sessions": sessions, "active_session": active}


@router.get("/cash/sessions")
def get_sessions(request: Request):
    with db(request) as conn:
        return _sessions(conn)


@router.post("/cash/sessions")
def save_session(payload: SessionInput, request: Request):
    with db(request) as conn:
        active = conn.execute("SELECT * FROM cash_sessions WHERE status='open'").fetchone()
        if payload.action == "open":
            if active:
                raise HTTPException(409, "Já existe um caixa aberto. Feche-o antes de abrir outro.")
            conn.execute(
                "INSERT INTO cash_sessions (id,opened_at,opening_cents,notes,status) VALUES (?,?,?,?,'open')",
                (_id(), utcnow(), payload.opening_cents, payload.notes),
            )
        else:
            if not active or (payload.session_id and payload.session_id != active["id"]):
                raise HTTPException(409, "O caixa informado não está aberto.")
            expected = active["opening_cents"] + _session_balance(conn, active["id"])
            conn.execute(
                "UPDATE cash_sessions SET closed_at=?,closing_cents=?,expected_cents=?,notes=?,status='closed' WHERE id=?",
                (utcnow(), payload.closing_cents, expected, payload.notes or active["notes"], active["id"]),
            )
        return _sessions(conn)


def _cash_entries(conn):
    entries = [dict(row) for row in conn.execute("SELECT * FROM cash_entries ORDER BY created_at DESC,id DESC")]
    balance = sum(item["amount_cents"] * (1 if item["kind"] in ("credit", "supply") else -1) for item in entries)
    return {"entries": entries, "balance_cents": balance}


@router.get("/cash/entries")
def get_cash_entries(request: Request):
    with db(request) as conn:
        return _cash_entries(conn)


@router.post("/cash/entries")
def create_cash_entry(payload: CashEntryInput, request: Request):
    with db(request) as conn:
        session = None
        if payload.session_id:
            session = _found(conn, "cash_sessions", payload.session_id, "Caixa não encontrado.")
            if session["status"] != "open":
                raise HTTPException(409, "Não é possível movimentar um caixa fechado.")
        else:
            session = conn.execute("SELECT * FROM cash_sessions WHERE status='open'").fetchone()
        conn.execute(
            "INSERT INTO cash_entries (id,created_at,kind,amount_cents,description,session_id,order_id) VALUES (?,?,?,?,?,?,NULL)",
            (_id(), utcnow(), payload.kind, payload.amount_cents, payload.description, session["id"] if session else None),
        )
        return _cash_entries(conn)


def record_sale(conn, order):
    """Registra recebimento explícito ``paid`` uma vez; pedido pendente não é receita."""
    order = dict(order)
    if order.get("payment_status") != "paid" or order["total_cents"] <= 0:
        return False
    debt = conn.execute("SELECT * FROM receivables WHERE order_id=?", (order["id"],)).fetchone()
    if debt:
        paid = conn.execute("SELECT COALESCE(SUM(amount_cents),0) FROM receivable_payments WHERE receivable_id=?", (debt["id"],)).fetchone()[0]
        if debt["amount_cents"] != order["total_cents"] or paid != debt["amount_cents"]:
            raise HTTPException(409, "Este pedido possui fiado. Registre o recebimento na tela de fiado.")
        # As baixas já geraram os créditos correspondentes, um por recibo.
        return False
    active = conn.execute("SELECT id FROM cash_sessions WHERE status='open'").fetchone()
    result = conn.execute(
        """INSERT INTO cash_entries (id,created_at,kind,amount_cents,description,session_id,order_id)
           VALUES (?,?,'credit',?,?,?,?) ON CONFLICT(order_id) DO NOTHING""",
        (_id(), utcnow(), order["total_cents"], f"Recebimento do pedido {order['id']}.",
         active["id"] if active else None, order["id"]),
    )
    return result.rowcount == 1


def _receivables(conn):
    rows = conn.execute(
        """SELECT r.*,COALESCE(SUM(p.amount_cents),0) AS paid_cents FROM receivables r
           LEFT JOIN receivable_payments p ON p.receivable_id=r.id
           GROUP BY r.id ORDER BY r.created_at DESC,r.id DESC"""
    ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["balance_cents"] = item["amount_cents"] - item["paid_cents"]
        item["status"] = "paid" if item["balance_cents"] == 0 else "partial" if item["paid_cents"] else "open"
        item["payments"] = [dict(payment) for payment in conn.execute(
            "SELECT * FROM receivable_payments WHERE receivable_id=? ORDER BY created_at,id", (item["id"],)
        )]
        results.append(item)
    return {"receivables": results}


@router.get("/receivables")
def get_receivables(request: Request):
    with db(request) as conn:
        return _receivables(conn)


@router.post("/receivables")
def create_receivable(payload: ReceivableInput, request: Request):
    with db(request) as conn:
        if payload.order_id:
            order = _found(conn, "orders", payload.order_id, "Pedido não encontrado.")
            if order["payment_status"] == "paid":
                raise HTTPException(409, "Este pedido já está pago.")
            if order["status"] in ("cancelled", "canceled"):
                raise HTTPException(409, "Pedido cancelado não pode gerar fiado.")
            if payload.amount_cents != order["total_cents"]:
                raise HTTPException(422, "O fiado vinculado deve corresponder ao total do pedido.")
            if conn.execute("SELECT 1 FROM receivables WHERE order_id=?", (payload.order_id,)).fetchone():
                raise HTTPException(409, "Este pedido já possui um fiado cadastrado.")
        conn.execute("INSERT INTO receivables VALUES (?,?,?,?,?,?,?,?)", (
            _id(), payload.customer_name, payload.phone, payload.amount_cents, payload.description,
            payload.due_date.isoformat() if payload.due_date else None, payload.order_id, utcnow(),
        ))
        return _receivables(conn)


@router.post("/receivables/{receivable_id}/payments")
def pay_receivable(receivable_id: str, payload: ReceivablePaymentInput, request: Request):
    with db(request) as conn:
        row = _found(conn, "receivables", receivable_id, "Fiado não encontrado.")
        existing = conn.execute("SELECT * FROM receivable_payments WHERE idempotency_key=?", (payload.idempotency_key,)).fetchone()
        if existing:
            if (existing["receivable_id"] != receivable_id or existing["amount_cents"] != payload.amount_cents
                    or existing["payment_method"] != payload.payment_method or existing["description"] != payload.description):
                raise HTTPException(409, "Esta identificação de pagamento já foi usada para outro recebimento.")
            return _receivables(conn)
        if row["order_id"]:
            order = _found(conn, "orders", row["order_id"], "Pedido não encontrado.")
            if order["status"] in ("cancelled", "canceled"):
                raise HTTPException(409, "Não é possível receber fiado de um pedido cancelado.")
        paid = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM receivable_payments WHERE receivable_id=?", (receivable_id,)
        ).fetchone()[0]
        if payload.amount_cents > row["amount_cents"] - paid:
            raise HTTPException(409, "O pagamento ultrapassa o saldo do fiado.")
        payment_id = _id()
        conn.execute("INSERT INTO receivable_payments VALUES (?,?,?,?,?,?,?)", (
            payment_id, receivable_id, payload.amount_cents, payload.payment_method, payload.description, utcnow(), payload.idempotency_key,
        ))
        active = conn.execute("SELECT id FROM cash_sessions WHERE status='open'").fetchone()
        conn.execute(
            """INSERT INTO cash_entries (id,created_at,kind,amount_cents,description,session_id,receivable_payment_id)
               VALUES (?,?,'credit',?,?,?,?)""",
            (_id(), utcnow(), payload.amount_cents, f"Recebimento de fiado: {row['customer_name']}.",
             active["id"] if active else None, payment_id),
        )
        if row["order_id"] and paid + payload.amount_cents == row["amount_cents"]:
            conn.execute("UPDATE orders SET payment_status='paid' WHERE id=?", (row["order_id"],))
            # A quitação pode ocorrer depois da entrega. Usa a mesma regra
            # transacional do recebimento comum, sem conceder pontos em dobro.
            from .marketing import apply_loyalty

            apply_loyalty(conn, order)
        return _receivables(conn)


def receivable_payment_total(conn, order_id):
    """Recebimentos de um pedido, usados para exigir estorno antes de cancelar."""
    return conn.execute(
        """SELECT COALESCE(SUM(p.amount_cents),0) FROM receivable_payments p
           JOIN receivables r ON r.id=p.receivable_id WHERE r.order_id=?""", (order_id,),
    ).fetchone()[0]


def has_receivable_payments(conn, order_id):
    """Impede cancelar um pedido com recebimento parcial sem resolver o estorno."""
    return receivable_payment_total(conn, order_id) > 0


def _maps_link(order):
    delivery = order.get("delivery")
    if delivery is None:
        delivery = json.loads(order.get("delivery_json", "{}"))
    location = delivery.get("location") or {}
    lat = location.get("latitude", location.get("lat", delivery.get("latitude")))
    lng = location.get("longitude", location.get("lng", delivery.get("longitude")))
    try:
        lat, lng = _decimal(lat), _decimal(lng)
        valid = lat.is_finite() and lng.is_finite() and -90 <= lat <= 90 and -180 <= lng <= 180
    except (ValueError, TypeError, ArithmeticError):
        valid = False
    if valid:
        destination = f"{lat},{lng}"
    else:
        parts = [delivery.get(key) for key in ("street", "house_number", "number", "neighborhood", "city")]
        destination = ", ".join(str(part).strip() for part in parts if part)
        if not destination:
            destination = str(delivery.get("address", "")).strip()
    if not destination:
        return None
    return "https://www.google.com/maps/dir/?" + urlencode({"api": "1", "destination": destination})


def _drivers(conn):
    drivers = []
    for row in conn.execute("SELECT * FROM drivers ORDER BY name COLLATE NOCASE"):
        driver = dict(row)
        driver["active"] = bool(driver["active"])
        orders = []
        for order in conn.execute(
            "SELECT * FROM orders WHERE courier_id=? AND status NOT IN ('delivered','cancelled','canceled') ORDER BY created_at",
            (driver["id"],),
        ):
            data = order_dict(order)
            orders.append({"id": order["id"], "customer_name": order["customer_name"],
                           "status": order["status"], "maps_url": _maps_link(data)})
        driver["orders"] = orders
        drivers.append(driver)
    return {"drivers": drivers}


@router.get("/drivers")
def get_drivers(request: Request):
    with db(request) as conn:
        return _drivers(conn)


@router.post("/drivers")
def save_driver(payload: DriverInput, request: Request):
    with db(request) as conn:
        if payload.id:
            _found(conn, "drivers", payload.id, "Entregador não encontrado.")
            conn.execute("UPDATE drivers SET name=?,phone=?,active=? WHERE id=?",
                         (payload.name, payload.phone, int(payload.active), payload.id))
        else:
            conn.execute("INSERT INTO drivers VALUES (?,?,?,?,?)", (_id(), payload.name, payload.phone, int(payload.active), utcnow()))
        return _drivers(conn)


@router.patch("/orders/{order_id}/driver")
def assign_driver(order_id: str, payload: DriverAssignment, request: Request):
    with db(request) as conn:
        order = _found(conn, "orders", order_id, "Pedido não encontrado.")
        if order["status"] in ("delivered", "cancelled", "canceled"):
            raise HTTPException(409, "Não é possível alterar o entregador de um pedido encerrado.")
        if payload.driver_id:
            driver = _found(conn, "drivers", payload.driver_id, "Entregador não encontrado.")
            if not driver["active"]:
                raise HTTPException(409, "O entregador está inativo.")
        conn.execute("UPDATE orders SET courier_id=? WHERE id=?", (payload.driver_id, order_id))
        result = order_dict(conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
        result["maps_url"] = _maps_link(result)
        return {"order": result, "drivers": _drivers(conn)["drivers"]}


def _printers(conn):
    printers = []
    for row in conn.execute("SELECT * FROM printer_profiles ORDER BY name COLLATE NOCASE"):
        item = dict(row)
        item["active"] = bool(item["active"])
        printers.append(item)
    return {"printers": printers, "print_method": "browser"}


@router.get("/printers")
def get_printers(request: Request):
    with db(request) as conn:
        return _printers(conn)


@router.post("/printers")
def save_printer(payload: PrinterInput, request: Request):
    with db(request) as conn:
        if payload.id:
            _found(conn, "printer_profiles", payload.id, "Perfil de impressão não encontrado.")
            conn.execute("UPDATE printer_profiles SET name=?,paper_width=?,copies=?,active=? WHERE id=?",
                         (payload.name, payload.paper_width, payload.copies, int(payload.active), payload.id))
        else:
            conn.execute("INSERT INTO printer_profiles VALUES (?,?,?,?,?,?)",
                         (_id(), payload.name, payload.paper_width, payload.copies, int(payload.active), utcnow()))
        return _printers(conn)
