import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import config
from app.db.database import get_db, check_connection, engine, Base
from app.models.tables import (Customer, Product, Order, Alert, Message, ConversationState)
from app.services import ai_service, order_flow, product_search
from app.services.order_flow import REASON_LABEL
from app.services.seed import seed

router = APIRouter()


# ------------------------------------------------------------------ shapes
class ChatIn(BaseModel):
    customer_phone: str
    message: str


class ReplyIn(BaseModel):
    text: str


class HandBackIn(BaseModel):
    send_order_summary: bool = False
    discount_percent: float = 0


class ProductIn(BaseModel):
    sku: str
    name: str
    brand: Optional[str] = None
    category: Optional[str] = None
    nicknames: Optional[str] = None
    price: float
    stock: int = 0


def iso(dt):
    return dt.isoformat() + "Z" if dt else None


def msg_out(m: Message):
    return {"id": m.id, "sender": m.sender, "sender_name": m.sender_name, "text": m.text,
            "meta": json.loads(m.meta) if m.meta else None, "created_at": iso(m.created_at)}


def product_out(p: Product):
    return {"id": p.id, "sku": p.sku, "name": p.name, "brand": p.brand, "category": p.category,
            "nicknames": p.nicknames, "price": p.price, "stock": p.stock}


def customer_out(c: Optional[Customer], phone: str = None):
    if not c:
        return {"phone": phone, "contact_name": None, "shop_name": "Unknown number", "known": False}
    return {"phone": c.phone, "contact_name": c.contact_name, "shop_name": c.shop_name, "known": True,
            "credit_limit": c.credit_limit, "credit_used": c.credit_used, "can_order": c.can_order,
            "salesperson": c.salesperson.name if c.salesperson else None}


# ------------------------------------------------------------------ health
@router.get("/health")
def health():
    ok = check_connection()
    return {
        "status": "ok" if ok else "error",
        "database": "connected" if ok else "not connected",
        "ai_mode": config.AI_MODE_LABEL,          # e.g. "Ollama (llama3.2:3b)" (shown in the top bar)
        "ai_provider": config.LLM_PROVIDER,
        "ai_enabled": config.AI_ENABLED,
        "chat_model": ai_service.chat_model() if config.AI_ENABLED else None,
        "embeddings": config.EMBEDDINGS_ENABLED,
        "embedding_model": ai_service.embed_model() if config.EMBEDDINGS_ENABLED else None,
    }


# ------------------------------------------------------------------ customer chat
@router.post("/chat")
def chat(body: ChatIn, db: Session = Depends(get_db)):
    if not body.customer_phone.strip() or not body.message.strip():
        raise HTTPException(400, "customer_phone and message are required")
    replies = order_flow.handle_message(db, body.customer_phone, body.message)
    return {"replies": [msg_out(m) for m in replies]}


@router.get("/customers")
def customers(db: Session = Depends(get_db)):
    return [customer_out(c) for c in db.query(Customer).order_by(Customer.id).all()]


@router.get("/conversations")
def conversations(db: Session = Depends(get_db)):
    phones = [r[0] for r in db.query(Message.customer_phone).distinct().all()]
    out = []
    for phone in phones:
        last = (db.query(Message).filter_by(customer_phone=phone)
                .order_by(Message.id.desc()).first())
        state = db.query(ConversationState).filter_by(customer_phone=phone).first()
        alert = db.query(Alert).filter_by(customer_phone=phone, kind="handoff", status="open").first()
        out.append({
            "customer": customer_out(db.query(Customer).filter_by(phone=phone).first(), phone),
            "mode": state.mode if state else "ai",
            "last_message": msg_out(last) if last else None,
            "open_alert": {"id": alert.id, "reason": alert.reason,
                           "reason_label": REASON_LABEL.get(alert.reason, alert.reason)} if alert else None,
        })
    out.sort(key=lambda c: (c["open_alert"] is None, -(c["last_message"]["id"] if c["last_message"] else 0)))
    return out


@router.get("/conversations/{phone}/messages")
def conversation_messages(phone: str, db: Session = Depends(get_db)):
    msgs = db.query(Message).filter_by(customer_phone=phone).order_by(Message.id).all()
    state = db.query(ConversationState).filter_by(customer_phone=phone).first()
    data = json.loads(state.data) if state and state.data else {}
    lines, total = order_flow.draft_lines(db, data.get("draft") or [], data.get("discount_percent", 0) or 0)
    return {
        "customer": customer_out(db.query(Customer).filter_by(phone=phone).first(), phone),
        "mode": state.mode if state else "ai",
        "stage": state.stage if state else "idle",
        "draft": {"items": lines, "total": total},
        "messages": [msg_out(m) for m in msgs],
    }


@router.delete("/conversations/{phone}")
def clear_conversation(phone: str, db: Session = Depends(get_db)):
    """Start a new chat: delete this customer's messages and conversation state.
    Products, orders and alerts are NOT touched."""
    deleted = db.query(Message).filter_by(customer_phone=phone).delete(synchronize_session=False)
    db.query(ConversationState).filter_by(customer_phone=phone).delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "messages_deleted": deleted}


# ------------------------------------------------------------------ salesperson dashboard
@router.post("/conversations/{phone}/takeover")
def takeover(phone: str, db: Session = Depends(get_db)):
    return {"messages": [msg_out(m) for m in order_flow.salesperson_takeover(db, phone)]}


@router.post("/conversations/{phone}/reply")
def salesperson_reply(phone: str, body: ReplyIn, db: Session = Depends(get_db)):
    if not body.text.strip():
        raise HTTPException(400, "text is required")
    return {"messages": [msg_out(m) for m in order_flow.salesperson_reply(db, phone, body.text.strip())]}


@router.post("/conversations/{phone}/handback")
def handback(phone: str, body: HandBackIn, db: Session = Depends(get_db)):
    msgs = order_flow.hand_back_to_ai(db, phone, body.send_order_summary, body.discount_percent)
    return {"messages": [msg_out(m) for m in msgs]}


@router.get("/alerts")
def alerts(status: str = "open", db: Session = Depends(get_db)):
    q = db.query(Alert)
    if status != "all":
        q = q.filter_by(status=status)
    out = []
    for a in q.order_by(Alert.id.desc()).all():
        out.append({
            "id": a.id, "kind": a.kind, "reason": a.reason,
            "reason_label": REASON_LABEL.get(a.reason, a.reason),
            "message": a.message, "status": a.status, "created_at": iso(a.created_at),
            "details": json.loads(a.details) if a.details else None,
            "salesperson": a.salesperson.name if a.salesperson else None,
            "customer": customer_out(db.query(Customer).filter_by(phone=a.customer_phone).first(), a.customer_phone),
        })
    return out


@router.post("/alerts/{alert_id}/dismiss")
def dismiss_alert(alert_id: int, db: Session = Depends(get_db)):
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "Alert not found")
    a.status = "resolved"
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ products
@router.get("/products")
def list_products(db: Session = Depends(get_db)):
    return [product_out(p) for p in db.query(Product).order_by(Product.id).all()]


@router.post("/products")
def add_product(body: ProductIn, db: Session = Depends(get_db)):
    if db.query(Product).filter_by(sku=body.sku).first():
        raise HTTPException(400, f"SKU {body.sku} already exists")
    p = Product(**body.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    _embed_product(db, p)
    return product_out(p)


@router.put("/products/{product_id}")
def update_product(product_id: int, body: ProductIn, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    db.commit()
    _embed_product(db, p)          # re-computed only if the product text changed
    return product_out(p)


def _embed_product(db: Session, p: Product):
    """RAG: keep this product's stored embedding up to date. A failure never blocks saving."""
    try:
        product_search.ensure_embeddings(db, [p])
    except Exception as e:  # noqa: BLE001
        db.rollback()
        print(f"⚠️  Could not compute the embedding for {p.sku}: {e}")


@router.delete("/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    p = db.get(Product, product_id)
    if not p:
        raise HTTPException(404, "Product not found")
    try:
        db.delete(p)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, f"{p.name} is used in existing orders, so it cannot be deleted. Set its stock to 0 instead.")
    return {"ok": True}


@router.get("/search")
def search(q: str, db: Session = Depends(get_db)):
    """See the RAG retrieval step on its own: what products match the words, with scores."""
    results = product_search.search(db.query(Product).all(), q, k=5)
    return [{"score": s, **product_out(p)} for p, s in results]


# ------------------------------------------------------------------ orders
@router.get("/orders")
def orders(db: Session = Depends(get_db)):
    out = []
    for o in db.query(Order).order_by(Order.id.desc()).all():
        out.append({
            "order_code": o.order_code, "status": o.status, "total": o.total, "path": o.path,
            "discount_percent": o.discount_percent, "created_at": iso(o.created_at),
            "customer": o.customer.shop_name if o.customer else None,
            "items": [{"name": i.product.name if i.product else "?", "quantity": i.quantity,
                       "unit_price": i.unit_price} for i in o.items],
        })
    return out


# ------------------------------------------------------------------ demo
@router.post("/demo/reset")
def demo_reset(db: Session = Depends(get_db)):
    """Delete everything and load fresh demo data (for a clean demo)."""
    db.close()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    from app.db.database import SessionLocal
    s = SessionLocal()
    try:
        seed(s)
        try:
            product_search.ensure_embeddings(s)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            print(f"⚠️  Could not compute embeddings after reset: {e}")
    finally:
        s.close()
    return {"ok": True}
