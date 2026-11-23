"""
The order workflow (the "brain"). Follows the proposal's 7 steps:
  1 Identity  2 Intent  3 Clarification  4 Validation  5 Confirmation  6 Execution  7 Feedback

Rules that are enforced HERE in code (not by the AI):
  - Price, stock and credit always come from the database.
  - An order is only created after the customer says YES to a summary.
  - Discount, big orders, low credit, "talk to a person", repeated confusion -> salesperson.
  - While a salesperson is in the chat, the AI stays quiet.
"""
import json
import re

from sqlalchemy.orm import Session

from app import config
from app.models.tables import (Customer, Product, Order, OrderItem, Alert, Message,
                               ConversationState, Salesperson)
from app.services import ai_service, product_search

REASON_LABEL = {
    "discount": "Discount request",
    "big_order": "Big order",
    "credit": "Not enough credit",
    "not_understood": "AI could not understand",
    "unknown_product": "Product not found",
    "wants_human": "Customer asked for a person",
    "unauthorised": "Account not allowed to order",
    "new_customer": "New customer (lead)",
    "order_placed": "Order placed by AI",
    "takeover": "Salesperson took over",
}


def naira(x: float) -> str:
    return f"₦{x:,.0f}"


# ------------------------------------------------------------------ helpers
def save_message(db: Session, phone: str, sender: str, text: str, meta=None, sender_name=None) -> Message:
    m = Message(customer_phone=phone, sender=sender, sender_name=sender_name, text=text,
                meta=json.dumps(meta) if meta else None)
    db.add(m)
    db.flush()
    return m


def get_state(db: Session, phone: str) -> ConversationState:
    st = db.query(ConversationState).filter_by(customer_phone=phone).first()
    if not st:
        st = ConversationState(customer_phone=phone, mode="ai", stage="idle", data="{}")
        db.add(st)
        db.flush()
    return st


def find_customer(db: Session, phone: str):
    return db.query(Customer).filter_by(phone=phone).first()


class Ctx:
    def __init__(self, db, phone, text, customer, state):
        self.db = db
        self.phone = phone
        self.text = text
        self.customer = customer
        self.state = state
        self.data = json.loads(state.data or "{}")
        self.replies = []
        self.parsed = {}

    def reply(self, text, meta=None):
        self.replies.append(save_message(self.db, self.phone, "ai", text, meta, "AI assistant"))

    def system(self, text):
        self.replies.append(save_message(self.db, self.phone, "system", text))

    def stage(self, s):
        self.state.stage = s

    def save(self):
        self.state.data = json.dumps(self.data)


def draft_lines(db: Session, draft: list, discount: float = 0):
    lines, total = [], 0.0
    for d in draft:
        p = db.get(Product, d["product_id"])
        if not p:
            continue
        unit = round(p.price * (1 - discount / 100), 2)
        line_total = unit * d["quantity"]
        total += line_total
        lines.append({"product_id": p.id, "name": p.name, "sku": p.sku, "quantity": d["quantity"],
                      "unit_price": unit, "line_total": line_total})
    return lines, total


# ------------------------------------------------------------------ entry point
def handle_message(db: Session, phone: str, text: str) -> list:
    """Customer sent a message. Returns the new AI/system messages."""
    phone = phone.strip()
    text = (text or "").strip()
    save_message(db, phone, "customer", text)
    state = get_state(db, phone)
    customer = find_customer(db, phone)
    ctx = Ctx(db, phone, text, customer, state)

    # The chat is never locked: the bot always answers (a hand-off only creates an alert).
    if state.mode != "ai":
        state.mode = "ai"           # chats locked by the old version are opened again
    if customer is None:
        handle_new_customer(ctx)
    else:
        process(ctx)

    ctx.save()
    db.commit()
    return ctx.replies


def handle_new_customer(ctx: Ctx):
    """Unknown phone number -> lead alert for the salesperson with the fewest customers."""
    sp = min(ctx.db.query(Salesperson).all(), key=lambda s: len(s.customers), default=None)
    ctx.data["lead_details"] = ctx.text
    escalate(ctx, "new_customer", salesperson=sp)


# ------------------------------------------------------------------ main flow
def process(ctx: Ctx):
    # STEP 2 - intent
    parsed = ai_service.extract(ctx.text, ctx.state.stage)
    ctx.parsed = parsed
    intent = parsed["intent"]
    number = parsed.get("choice_number")
    stage = ctx.state.stage

    # Policy first: these always go to the salesperson
    if not ctx.customer.can_order:
        return escalate(ctx, "unauthorised")
    if parsed["wants_human"]:
        return escalate(ctx, "wants_human")
    if parsed["wants_discount"]:
        if parsed["items"]:
            resolve_quietly(ctx, parsed["items"])
        return escalate(ctx, "discount")

    if intent == "cancel":
        clear_order(ctx)
        return ctx.reply("Okay, I've cancelled this order. Message me any time you want to order. 👍")

    if intent == "change" and stage in {"awaiting_choice", "awaiting_quantity"}:
        clear_order(ctx)
        return ctx.reply("No problem. Tell me the products and quantities you want, "
                         "for example: \"10 HP laptops and 2 chargers\".")

    if stage == "awaiting_choice":
        # "2, 10 pieces" / "2 x 10" = option 2, quantity 10
        m = re.fullmatch(r"\D*?(\d{1,3})\s*[,.\-x×:/ ]+\s*(\d{1,6})\s*[a-z ]*", ctx.text.strip().lower())
        if m:
            queue = ctx.data.setdefault("queue", [])
            if not queue:
                queue.append({"product_text": "choice", "quantity": None})
            queue[0]["quantity"] = int(m.group(2))
            return handle_choice(ctx, int(m.group(1)))
        if number is not None:
            return handle_choice(ctx, number)
    if stage == "awaiting_quantity" and number is not None:
        return handle_quantity(ctx, number)

    if stage == "awaiting_confirmation":
        if intent == "confirm":
            return place_order(ctx)
        if intent == "change":
            clear_order(ctx)
            return ctx.reply("No problem. Tell me the products and quantities you want, "
                             "for example: \"10 HP laptops and 2 chargers\".")
        # "no wait", "not yet", "don't confirm": keep the order on hold, never place it
        if ai_service.NEGATIVE.search(ctx.text) and not re.search(r"\d", ctx.text):
            return ctx.reply("Okay, I have NOT placed the order. 👍\n"
                             "Reply YES when you are ready, or CHANGE to modify it.")

    if intent == "confirm" and stage == "idle":
        return ctx.reply("There's no order waiting for confirmation yet. What would you like to order?")
    if intent == "greeting":
        return ctx.reply("Hi! 👋 How can I help you today?")
    if intent == "status":
        return order_status(ctx)
    if intent == "reorder":
        return reorder(ctx)
    if intent == "enquiry":
        return product_enquiry(ctx)
    if intent == "order" and parsed["items"]:
        ctx.data.update({"draft": [], "queue": parsed["items"], "clarified": False,
                         "tries": ctx.data.get("tries", 0), "approved": False, "discount_percent": 0})
        return process_queue(ctx)

    return not_understood(ctx)


def not_understood(ctx: Ctx):
    ctx.data["tries"] = ctx.data.get("tries", 0) + 1
    if ctx.data["tries"] >= config.MAX_CLARIFY_TRIES:
        return escalate(ctx, "not_understood")
    if ctx.state.stage == "awaiting_choice":
        return ctx.reply("Please reply with the number of the product you want (for example: 1).")
    if ctx.state.stage == "awaiting_quantity":
        return ctx.reply("Please reply with the quantity as a number (for example: 10).")
    return escalate(ctx, "not_understood")


# ------------------------------------------------------------------ STEP 3: find products (RAG)
# General words: they say WHAT KIND of product, not WHICH one. With only these words we never pick
# one product by ourselves when several match.
GENERIC_WORDS = {
    "laptop", "notebook", "computer", "pc", "printer", "mouse", "mice", "charger", "adapter", "ups",
    "inverter", "flash", "drive", "usb", "pendrive", "stick", "storage", "accessory", "accessorie", "power",
    "wireless", "ink", "tank", "small", "device", "product", "item", "thing", "one", "model", "brand", "type",
}
# Words of a question, removed before searching ("what laptops do you have" -> "laptops")
ENQUIRY_WORDS = {
    "what", "which", "wetin", "show", "list", "see", "available", "provide", "sell", "selling", "have", "got",
    "stock", "catalog", "catalogue", "option", "price", "prices", "how", "much", "do", "doe", "is", "are",
    "there", "any", "let", "know", "tell", "u", "will", "can", "your", "all", "waht", "wat", "teh", "da",
}
CATEGORY_SYNONYMS = {"notebook": "laptop", "computer": "laptop", "pc": "laptop"}


def _tokens(text: str) -> list:
    return [w for w in product_search.normalize(text).split() if w not in product_search.STOP_WORDS]


def product_words(p: Product) -> set:
    """Brand / model words of a product: 'hp', '250', 'g9', 'probook', 'v15' ... (no general words)."""
    text = " ".join(filter(None, [p.brand, p.name, (p.nicknames or "").replace(",", " ")]))
    return {w for w in _tokens(text) if w not in GENERIC_WORDS}


def specific_words(text: str, products: list) -> set:
    """Brand / model words the customer used ('hp', 'dell', 'v15'). Plain numbers are quantities, not models."""
    known = set().union(*(product_words(p) for p in products)) if products else set()
    return {w for w in _tokens(text) if w in known and not w.isdigit()}


def choose_product(ctx: Ctx, product_text: str, products: list):
    """Returns (product, options).
    product = the one product to use, or None when the customer must pick from `options` (best first).
    Rule: pick one product by ourselves ONLY if just one product matches, or the customer named a brand/model."""
    cands = product_search.search(products, product_text, k=8)
    if not cands:
        return None, []
    best = cands[0][1]
    options = [(p, s) for p, s in cands if s >= max(0.35, best - 0.35)]   # below 0.35 = not in our catalog
    category = _only_category_words(product_text, cands)
    if category:   # "laptops" -> only laptops (not the "Laptop Charger")
        options = [(p, s) for p, s in options if product_search.normalize(p.category or "") == category]
    spec = specific_words(product_text, products)
    if spec:       # "hp laptop" -> only HP products
        named = [(p, s) for p, s in cands if spec & product_words(p) and s >= 0.2]
        if named:
            options = named
    if not options:
        return None, []
    if len(options) == 1:
        return options[0][0], options
    if not spec:
        return None, options          # general word + several matches -> ALWAYS show the list
    # Brand/model named and still several fit: clear search winner, else let the AI reason (RAG).
    (p1, s1), (_, s2) = options[0], options[1]
    if s1 >= 0.45 and (s1 - s2) >= 0.15:
        return p1, options
    if config.AI_ENABLED:
        p, confident = ai_service.pick_product(product_text, ctx.text, options)
        if p and confident and spec & product_words(p):   # AI follows the same rule
            return p, options
    return None, options


def _only_category_words(product_text: str, cands: list):
    """'laptops' / 'printer' -> that category, when 2+ retrieved products are in it. Else None."""
    words = [w for w in product_search.normalize(product_text).split() if w not in product_search.STOP_WORDS]
    if not words:
        return None
    for cat in {product_search.normalize(p.category or "") for p, _ in cands}:
        if cat and all(w in cat.split() for w in words):
            if sum(1 for p, _ in cands if product_search.normalize(p.category or "") == cat) >= 2:
                return cat
    return None


def add_to_draft(ctx: Ctx, product_id: int, qty: int, replace=False):
    draft = ctx.data.setdefault("draft", [])
    for d in draft:
        if d["product_id"] == product_id:
            d["quantity"] = qty if replace else d["quantity"] + qty
            return
    draft.append({"product_id": product_id, "quantity": qty})


def resolve_quietly(ctx: Ctx, items: list):
    """Used before a hand-off: keep what we clearly understood, so the salesperson sees it."""
    products = ctx.db.query(Product).all()
    ctx.data["draft"] = []
    for it in items:
        p, _ = choose_product(ctx, it["product_text"], products)
        if p and it.get("quantity"):
            add_to_draft(ctx, p.id, it["quantity"])


UNITS = {"laptop": ("laptop", "laptops"), "printer": ("printer", "printers")}


def unit(p: Product, qty: int = 1) -> str:
    """How we count this product: 'laptop' / 'printers' / 'piece' ..."""
    one, many = UNITS.get((p.category or "").strip().lower(), ("piece", "pieces"))
    return one if qty == 1 else many


def process_queue(ctx: Ctx):
    products = ctx.db.query(Product).all()
    queue = ctx.data.setdefault("queue", [])
    while queue:
        item = queue[0]
        chosen, options = choose_product(ctx, item["product_text"], products)
        if not chosen:
            options = options[:10]
            if not options:
                queue.pop(0)
                return escalate(ctx, "unknown_product")
            ctx.data["clarified"] = True
            ctx.data["choices"] = [p.id for p, _ in options]
            ctx.stage("awaiting_choice")
            qty = item.get("quantity")
            # price comes from the database
            rows = []
            for i, (p, _) in enumerate(options):
                row = f"{i + 1}. {p.name} – {naira(p.price)} per {unit(p)}"
                if qty:
                    row += f" – {qty} × = {naira(p.price * qty)}"
                rows.append(row)
            kinds = {unit(p, 2) for p, _ in options}
            title = f"Here are our {kinds.pop()}:" if len(kinds) == 1 and "pieces" not in kinds else "Here are our products:"
            return ctx.reply(
                title + "\n" + "\n".join(rows) + "\nReply with the number you want.",
                {"type": "choices", "options": [{"n": i + 1, "name": p.name} for i, (p, _) in enumerate(options)]},
            )
        if not item.get("quantity"):
            ctx.data["clarified"] = True
            ctx.data["pending_product"] = chosen.id
            ctx.data["pending_from_queue"] = True
            ctx.stage("awaiting_quantity")
            return ctx.reply(f"How many {chosen.name} do you want?")
        add_to_draft(ctx, chosen.id, int(item["quantity"]))
        queue.pop(0)
    return validate_and_summarize(ctx)


def product_enquiry(ctx: Ctx):
    """Customer asks what we have ('what laptops do you have'): list ALL matching products from the DB."""
    products = ctx.db.query(Product).order_by(Product.id).all()
    words = set(_tokens(ctx.text))
    by_cat = {product_search.normalize(p.category or ""): p.category for p in products if p.category}
    cats = {w for w in words if w in by_cat} | {CATEGORY_SYNONYMS[w] for w in words
                                                if CATEGORY_SYNONYMS.get(w) in by_cat}
    spec = specific_words(ctx.text, products)

    if cats:                                   # "what laptops ..." -> every laptop
        matches = [p for p in products if product_search.normalize(p.category or "") in cats]
    elif spec:                                 # "what hp products ..." -> every HP product
        matches = [p for p in products if spec & product_words(p)]
    else:                                      # "do you have mouse" -> search
        query = " ".join(w for w in _tokens(ctx.text) if w not in ENQUIRY_WORDS)
        if not query:                          # "what do you have?"
            kinds = ", ".join(sorted(set(by_cat.values())))
            return ctx.reply(f"We sell: {kinds}. Which would you like to see?")
        _, options = choose_product(ctx, query, products)
        matches = [p for p, _ in options]
    if cats and spec:                          # "what dell laptops ..." -> only Dell laptops
        matches = [p for p in matches if spec & product_words(p)] or matches
    if not matches:
        return escalate(ctx, "unknown_product")

    matches = matches[:10]
    kinds = {unit(p, 2) for p in matches}
    title = f"Here are the {kinds.pop()} we have:" if len(kinds) == 1 and "pieces" not in kinds \
        else "Here are the products we have:"
    # price and stock come from the database
    rows = [f"{i + 1}. {p.name} – {naira(p.price)} – "
            + (f"{p.stock:,} in stock" if p.stock > 0 else "out of stock") for i, p in enumerate(matches)]
    ctx.data.update({"draft": [], "queue": [{"product_text": "enquiry", "quantity": None}],
                     "choices": [p.id for p in matches], "clarified": True, "approved": False,
                     "discount_percent": 0, "tries": 0})
    ctx.stage("awaiting_choice")
    return ctx.reply(title + "\n" + "\n".join(rows) + "\nWhich one would you like, and how many?",
                     {"type": "choices", "options": [{"n": i + 1, "name": p.name} for i, p in enumerate(matches)]})


def handle_choice(ctx: Ctx, n: int):
    ids = ctx.data.get("choices") or []
    if not (1 <= n <= len(ids)):
        return ctx.reply(f"Please reply with a number from 1 to {len(ids)}.")
    pid = ids[n - 1]
    queue = ctx.data.get("queue") or []
    item = queue[0] if queue else {"quantity": None}
    ctx.data["choices"] = []
    if not item.get("quantity"):
        p = ctx.db.get(Product, pid)
        ctx.data["pending_product"] = pid
        ctx.data["pending_from_queue"] = True
        ctx.stage("awaiting_quantity")
        return ctx.reply(f"Great. How many {p.name} do you want?")
    add_to_draft(ctx, pid, int(item["quantity"]))
    if queue:
        queue.pop(0)
    return process_queue(ctx)


def handle_quantity(ctx: Ctx, n: int):
    if n <= 0:
        return ctx.reply("Please send a quantity of 1 or more.")
    pid = ctx.data.get("pending_product")
    if not pid:
        return not_understood(ctx)
    from_queue = ctx.data.get("pending_from_queue")
    add_to_draft(ctx, pid, n, replace=not from_queue)
    if from_queue and ctx.data.get("queue"):
        ctx.data["queue"].pop(0)
    ctx.data["pending_product"] = None
    return process_queue(ctx)


# ------------------------------------------------------------------ STEP 4 + 5: validate, summary
def validate_and_summarize(ctx: Ctx):
    draft = ctx.data.get("draft") or []
    notes = []
    for d in list(draft):
        p = ctx.db.get(Product, d["product_id"])
        if p.stock <= 0:
            draft.remove(d)
            notes.append(f"Sorry, {p.name} is out of stock right now.")
        elif p.stock < d["quantity"]:
            ctx.data["pending_product"] = p.id
            ctx.data["pending_from_queue"] = False
            ctx.stage("awaiting_quantity")
            return ctx.reply(f"Sorry, we have only {p.stock} {p.name} in stock. How many would you like?")
    if not draft:
        ctx.stage("idle")
        return ctx.reply("\n".join(notes))

    discount = ctx.data.get("discount_percent", 0) or 0
    lines, total = draft_lines(ctx.db, draft, discount)

    if not ctx.data.get("approved"):
        if total >= config.BIG_ORDER_AMOUNT or any(l["quantity"] >= config.BIG_ORDER_QTY for l in lines):
            return escalate(ctx, "big_order")
        available = (ctx.customer.credit_limit or 0) - (ctx.customer.credit_used or 0)
        if total > available:
            return escalate(ctx, "credit")

    ctx.stage("awaiting_confirmation")
    ctx.data["tries"] = 0
    # name, price and total: all from the database
    text_lines = []
    for l in lines:
        p = ctx.db.get(Product, l["product_id"])
        text_lines += [f"{l['name']} – {naira(l['unit_price'])} per {unit(p)}",
                       f"{l['quantity']} {unit(p, l['quantity'])} = {naira(l['line_total'])}"]
    if len(lines) > 1 or discount:
        text_lines.append(f"Total: {naira(total)}" + (f" (after {discount:g}% discount)" if discount else ""))
    msg = "\n".join(notes + text_lines + ["Reply YES to confirm or CHANGE to modify."])
    return ctx.reply(msg, {"type": "order_summary", "items": lines, "total": total, "discount_percent": discount})


# ------------------------------------------------------------------ STEP 6 + 7: place order, feedback
def place_order(ctx: Ctx):
    draft = ctx.data.get("draft") or []
    for d in draft:  # re-check stock at the last moment
        p = ctx.db.get(Product, d["product_id"])
        if p.stock < d["quantity"]:
            return validate_and_summarize(ctx)
    discount = ctx.data.get("discount_percent", 0) or 0
    lines, total = draft_lines(ctx.db, draft, discount)
    path = "C" if ctx.data.get("approved") else ("B" if ctx.data.get("clarified") else "A")

    order = Order(customer_id=ctx.customer.id, status="confirmed", total=total,
                  discount_percent=discount, path=path)
    ctx.db.add(order)
    ctx.db.flush()
    order.order_code = f"ORD-{1000 + order.id}"
    for l in lines:
        ctx.db.add(OrderItem(order_id=order.id, product_id=l["product_id"], quantity=l["quantity"],
                             unit_price=l["unit_price"]))
        p = ctx.db.get(Product, l["product_id"])
        p.stock -= l["quantity"]
    ctx.customer.credit_used = (ctx.customer.credit_used or 0) + total

    sp = ctx.customer.salesperson
    summary = ", ".join(f"{l['quantity']} × {l['name']}" for l in lines)
    ctx.db.add(Alert(kind="notice", customer_phone=ctx.phone, salesperson_id=sp.id if sp else None,
                     reason="order_placed", status="open",
                     message=f"{order.order_code} · {ctx.customer.shop_name}: {summary} · {naira(total)} (Path {path})",
                     details=json.dumps({"order_code": order.order_code, "items": lines, "total": total})))
    clear_order(ctx)
    return ctx.reply(f"✅ Order placed! Order ID: {order.order_code}. Total: {naira(total)}",
                     {"type": "order_placed", "order_code": order.order_code, "total": total})


def clear_order(ctx: Ctx):
    ctx.data.update({"draft": [], "queue": [], "choices": [], "pending_product": None, "tries": 0,
                     "approved": False, "discount_percent": 0, "clarified": False})
    ctx.stage("idle")


def order_status(ctx: Ctx):
    order = (ctx.db.query(Order).filter_by(customer_id=ctx.customer.id)
             .order_by(Order.created_at.desc(), Order.id.desc()).first())
    if not order:
        return ctx.reply("I can't find any orders for your account yet.")
    count = sum(i.quantity for i in order.items)
    return ctx.reply(f"Your last order {order.order_code} ({order.created_at:%d %b %Y}): "
                     f"{count} item(s), total {naira(order.total)}. Status: {order.status.upper()}.")


def reorder(ctx: Ctx):
    order = (ctx.db.query(Order).filter_by(customer_id=ctx.customer.id)
             .order_by(Order.created_at.desc(), Order.id.desc()).first())
    if not order or not order.items:
        return ctx.reply("I can't find a previous order. What would you like to order?")
    ctx.data.update({"draft": [], "queue": [], "clarified": False, "tries": 0, "approved": False,
                     "discount_percent": 0})
    for it in order.items:
        add_to_draft(ctx, it.product_id, it.quantity)
    ctx.reply(f"Sure! Your last order was {order.order_code}. I've checked today's price and stock:")
    return validate_and_summarize(ctx)


# ------------------------------------------------------------------ hand-off to salesperson
HANDOFF_REPLY = "Thank you. Our salesperson will contact you shortly. 🙏"


def escalate(ctx: Ctx, reason: str, salesperson=None):
    """The bot can't handle this: save an alert for the salesperson and tell the customer.
    The chat is NOT locked — the bot keeps answering the next messages."""
    sp = salesperson or (ctx.customer.salesperson if ctx.customer else None)
    lines, total = draft_lines(ctx.db, ctx.data.get("draft") or [], 0)
    ctx.db.add(Alert(kind="handoff", customer_phone=ctx.phone, salesperson_id=sp.id if sp else None,
                     reason=reason, message=ctx.text, status="open",
                     details=json.dumps({"draft": lines, "total": total,
                                         "lead_details": ctx.data.get("lead_details")})))
    clear_order(ctx)   # nothing is left waiting for YES, so no order can be placed by mistake
    ctx.reply(HANDOFF_REPLY, {"type": "handoff", "reason": reason})


# ------------------------------------------------------------------ salesperson actions
def salesperson_takeover(db: Session, phone: str):
    state = get_state(db, phone)
    customer = find_customer(db, phone)
    sp = customer.salesperson if customer else None
    out = []
    if state.mode != "human":
        state.mode = "human"
        out.append(save_message(db, phone, "system", f"{sp.name if sp else 'Salesperson'} joined the chat"))
    db.commit()
    return out


def salesperson_reply(db: Session, phone: str, text: str):
    state = get_state(db, phone)
    customer = find_customer(db, phone)
    alert = db.query(Alert).filter_by(customer_phone=phone, kind="handoff", status="open").first()
    sp = (customer.salesperson if customer else None) or (alert.salesperson if alert else None)
    name = sp.name if sp else "Salesperson"
    out = []
    if state.mode != "human":
        state.mode = "human"
        out.append(save_message(db, phone, "system", f"{name} joined the chat"))
    out.append(save_message(db, phone, "salesperson", text, sender_name=name))
    db.commit()
    return out


def hand_back_to_ai(db: Session, phone: str, send_order_summary: bool = False, discount_percent: float = 0):
    state = get_state(db, phone)
    customer = find_customer(db, phone)
    for a in db.query(Alert).filter_by(customer_phone=phone, kind="handoff", status="open").all():
        a.status = "resolved"
    state.mode = "ai"
    ctx = Ctx(db, phone, "", customer, state)
    ctx.system("Chat handed back to AI assistant")
    if send_order_summary and customer and ctx.data.get("draft"):
        ctx.data["approved"] = True                     # salesperson approved: skip big-order/credit checks
        ctx.data["discount_percent"] = max(0.0, min(float(discount_percent or 0), 100.0))
        validate_and_summarize(ctx)
    else:
        ctx.stage("idle")
    ctx.save()
    db.commit()
    return ctx.replies
