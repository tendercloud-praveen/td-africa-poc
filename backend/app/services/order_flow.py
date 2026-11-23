"""
The order workflow (the "brain"). Follows the proposal's 7 steps:
  1 Identity  2 Intent  3 Clarification  4 Validation  5 Confirmation  6 Execution  7 Feedback

Rules that are enforced HERE in code (not by the AI):
  - Price and stock always come from the database.
  - An order is only created after the customer says YES to the quotation.
  - Available credit is NOT checked: orders above the customer's credit are placed too.
  - Discount (offered), big orders (approval), "talk to a person", repeated confusion -> salesperson.
  - While a salesperson is in the chat, the AI stays quiet.
"""
import json
import re

from sqlalchemy.orm import Session

from app import config
from app.models.tables import (Customer, Product, Order, OrderItem, Alert, Message,
                               ConversationState, Salesperson)
from app.services import ai_service, message_parser, product_search

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
    "out_of_stock": "Out of stock",
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


def get_state(db: Session, phone: str, lock: bool = False) -> ConversationState:
    """lock=True: this chat's state row is locked until the message is fully handled, so two messages of
    the same customer (e.g. a double click on YES) are handled one after the other -> no duplicate orders."""
    q = db.query(ConversationState).filter_by(customer_phone=phone)
    st = (q.with_for_update() if lock else q).first()
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
                      "unit_price": unit, "line_total": line_total, "stock": p.stock})
    return lines, total


# ------------------------------------------------------------------ entry point
def handle_message(db: Session, phone: str, text: str) -> list:
    """Customer sent a message. Returns the new AI/system messages."""
    phone = phone.strip()
    text = (text or "").strip()
    save_message(db, phone, "customer", text)
    state = get_state(db, phone, lock=True)
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
NUMBER_STAGES = {"awaiting_choice", "awaiting_quantity", "awaiting_confirmation", "awaiting_edit"}


def process(ctx: Ctx):
    stage = ctx.state.stage
    # The bot asked a question ("How many?", "Which one?", the order summary): a reply that is just a
    # number with filler words ("6 I want", "Give me 6") or an edit request ("edit", "change quantity")
    # is read exactly by the rules, so the LLM is not needed for it.
    edit_kind = edit_request(ctx.text)
    quick_number = reply_number(ctx.text) if stage in NUMBER_STAGES else None
    place_words = bool(PLACE_ORDER_RE.fullmatch(" ".join(re.sub(r"[^a-z ]+", " ", ctx.text.lower()).split())))

    # STEP 2 - intent
    if quick_number is not None or edit_kind is not None or place_words:
        parsed = message_parser.parse(ctx.text, stage)
        if place_words:                        # "Yes, place order" / "place my order" = YES
            parsed["intent"], parsed["items"] = "confirm", []
    else:
        parsed = ai_service.extract(ctx.text, stage)
    ctx.parsed = parsed
    intent = parsed["intent"]
    number = parsed.get("choice_number")
    if number is None:
        number = quick_number

    # Policy first
    if not ctx.customer.can_order:
        return escalate(ctx, "unauthorised")

    # The answer to "Would you like me to connect you with a salesperson?"
    offer = ctx.data.pop("pending_offer", None)
    if offer:
        if intent == "confirm" or parsed["wants_human"]:
            return escalate(ctx, offer, accepted=True)
        if intent != "cancel" and ai_service.NEGATIVE.search(ctx.text) and not re.search(r"\d", ctx.text):
            return decline_offer(ctx)
        # anything else: the offer is dropped and the message is handled normally

    # The customer asks for a person: connect them, with the order details (no extra question needed).
    if parsed["wants_human"]:
        if stage == "awaiting_approval":       # the salesperson already has the quotation for approval
            withdraw_approval(ctx)
        return escalate(ctx, "wants_human", accepted=True)
    # Discount: the bot never invents or changes a price. It offers the salesperson first.
    if parsed["wants_discount"]:
        if parsed["items"]:
            resolve_quietly(ctx, parsed["items"])
        # The salesperson is notified at once (alert with the order details), without asking first.
        return escalate(ctx, "discount", reply="Our salesperson will contact you shortly.")

    # Quotation options: 1 = Yes, Place Order / 2 = Edit Order / 3 = Cancel Order
    if stage == "awaiting_confirmation" and number is not None:
        if number == 1:
            intent = "confirm"
        elif number == 2:
            edit_kind = "any"
        elif number == 3:
            intent = "cancel"
        else:
            return ctx.reply("Please reply 1 (Yes, Place Order), 2 (Edit Order) or 3 (Cancel Order).")
        number = None

    if intent == "cancel":
        if stage == "awaiting_approval":
            withdraw_approval(ctx)
        clear_order(ctx)                       # nothing is saved, stock is not touched
        return ctx.reply("Okay, I've cancelled this order. Nothing was ordered. "
                         "Message me any time you want to order. 👍")

    # The order is waiting for the salesperson's approval (credit / big order)
    if stage == "awaiting_approval":
        if intent == "confirm":
            return ctx.reply("Your order is still waiting for approval from your salesperson, so it is not placed yet. "
                             "I'll send you the quotation to confirm once it is approved. "
                             "You can also reply EDIT to change it or CANCEL to cancel it.")
        if edit_kind is not None or intent == "change":
            withdraw_approval(ctx)
            ctx.stage("awaiting_confirmation")
            return start_edit(ctx, edit_kind or "any")

    # "Edit" / "change quantity" after "we have only 8 available": ask the quantity again
    if stage == "awaiting_quantity" and edit_kind in ("quantity", "any") and ctx.data.get("pending_product"):
        p = ctx.db.get(Product, ctx.data["pending_product"])
        return ask_quantity(ctx, p, f"How many {p.name} would you like? We have {p.stock:,} available.",
                            from_queue=bool(ctx.data.get("pending_from_queue")),
                            requested=ctx.data.get("requested_quantity"))

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
    if stage == "awaiting_quantity":
        if number is not None:
            return handle_quantity(ctx, number)
        # "Yes" / "OK" after "we have only 8 available. Would you like to order 8 ...?" -> order 8
        if intent == "confirm" and ctx.data.get("requested_quantity") and ctx.data.get("available_stock"):
            return handle_quantity(ctx, int(ctx.data["available_stock"]))
        # "I need 6 Dell laptops" while we asked how many Dell Inspiron 3520: the same product with a
        # new quantity -> a quantity correction, not a new search. Another product -> a new order (below).
        n = same_product_quantity(ctx, parsed)
        if n is not None:
            return handle_quantity(ctx, n)

    if stage in {"awaiting_confirmation", "awaiting_edit"}:
        no = intent == "change" and message_parser._clean(ctx.text) in NO_WORDS
        if intent == "confirm":
            return place_order(ctx) if stage == "awaiting_confirmation" else validate_and_summarize(ctx)
        if no and stage == "awaiting_confirmation":
            clear_order(ctx)                       # nothing was saved, stock is not touched
            return ctx.reply("Okay, I've cancelled this order. Nothing was ordered. "
                             "Message me any time you want to order. 👍")
        if no:                                     # "no" while editing: keep the order as it was
            return validate_and_summarize(ctx)
        if number is not None:                     # "5" while editing = new quantity
            return edit_quantity(ctx, number)
        # A product message: "HP ProBook" (after "change product"), "make it 2 HP ProBook instead",
        # "I need 6 Dell laptops" (the Dell already in the order) -> change THIS order.
        items = parsed["items"]
        if edit_kind is None and intent != "cancel" and (stage == "awaiting_edit" or INSTEAD_RE.search(ctx.text.lower())):
            items = items or message_parser.parse(ctx.text, stage)["items"]
            if not items and stage == "awaiting_edit":
                items = [{"product_text": ctx.text, "quantity": None}]
        if items and edit_kind is None and (stage == "awaiting_edit" or ADD_RE.search(ctx.text.lower())
                                            or edits_current_order(ctx, items)):
            return apply_edit(ctx, items)
        if edit_kind is not None or (intent == "change" and stage == "awaiting_confirmation"):
            return start_edit(ctx, edit_kind or "any")
        # "no wait", "not yet", "don't confirm": keep the order on hold, never place it
        if stage == "awaiting_confirmation" and ai_service.NEGATIVE.search(ctx.text) and not re.search(r"\d", ctx.text):
            return ctx.reply("Okay, I have NOT placed the order. 👍\n"
                             "Reply CONFIRM when you are ready, EDIT to change it, or CANCEL.")

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


NO_WORDS = {"no", "nope", "nah", "no thanks", "no thank you"}   # at the summary: cancel (CHANGE = modify)

# Words that may come with a number when the customer answers "How many?" or "Which one?".
# Product words are NOT here: "6 Dell laptops" is checked by same_product_quantity instead.
REPLY_WORDS = {
    "i", "we", "want", "need", "wan", "select", "choose", "pick", "take", "give", "me", "us", "send",
    "product", "option", "item", "number", "the", "please", "pls", "plz", "abeg", "only", "just", "is",
    "are", "fine", "ok", "okay", "that", "will", "do", "make", "it", "am", "quantity", "qty", "piece",
    "pieces", "pc", "pcs", "unit", "units", "x", "of", "them", "let", "go", "with", "for", "now", "sir",
    "ma", "oga", "kindly", "would", "like", "d", "ll", "m", "my", "order", "buy", "get", "nos", "then",
}


def reply_number(text: str):
    """'6' / '6 I want' / 'I want 6' / 'Give me 6' / '6 pieces' / 'Select product 1' -> the number.
    None if there is not exactly one number, or if any other word (like a product) is in the message."""
    nums = []
    for tok in re.findall(r"\d+|[a-z]+", (text or "").lower()):
        if tok.isdigit():
            nums.append(int(tok))
        elif tok in message_parser.NUMBER_WORDS:
            nums.append(message_parser.NUMBER_WORDS[tok])
        elif tok not in REPLY_WORDS:
            return None
    return nums[0] if len(nums) == 1 else None


def same_product_quantity(ctx: Ctx, parsed: dict):
    """The new quantity, if the message asks for the product we are waiting on ('I need 6 Dell laptops'
    while asking about the Dell Inspiron 3520). Uses the same product search; None for another product."""
    pid = ctx.data.get("pending_product")
    items = parsed.get("items") or []
    if not pid or parsed.get("intent") != "order" or len(items) != 1 or not items[0].get("quantity"):
        return None
    chosen, options = choose_product(ctx, items[0]["product_text"], ctx.db.query(Product).all())
    if (chosen and chosen.id == pid) or (not chosen and any(p.id == pid for p, _ in options)):
        return int(items[0]["quantity"])
    return None


def ask_quantity(ctx: Ctx, p: Product, question: str, from_queue: bool, requested=None):
    """Ask "How many?" and remember the product, its stock, the quantity asked for and the question,
    so the next message is read as the answer to this question."""
    ctx.data.update({"pending_product": p.id, "pending_product_name": p.name, "available_stock": p.stock,
                     "requested_quantity": requested, "pending_from_queue": from_queue,
                     "last_question": question})
    ctx.stage("awaiting_quantity")
    return ctx.reply(question)


# ------------------------------------------------------------------ edit the order at the summary
EDIT_RE = re.compile(r"(?:i want to |i d like to |i would like to |let me |can i |pls |please )?"
                     r"(?:edit|change|modify|update|adjust)"
                     r"(?: my| the| this)?(?: order| quantity| qty| product| item| it| amount| number| details)?"
                     r"(?: please| pls)?")
INSTEAD_RE = re.compile(r"\b(instead|change|replace|switch|make it)\b")
PLACE_ORDER_RE = re.compile(r"(?:yes |ok |okay )?(?:please )?(?:place|confirm|submit)(?: my| the| this)? order(?: please| now)?")
ADD_RE = re.compile(r"\b(add|also|too|plus|another|extra)\b")


def edit_request(text: str):
    """'edit' / 'change quantity' / 'change product' / 'edit my order' -> 'quantity' / 'product' / 'any'.
    None for any other message."""
    t = " ".join(re.sub(r"[^a-z ]+", " ", (text or "").lower()).split())
    if not EDIT_RE.fullmatch(t):
        return None
    if re.search(r"\b(quantity|qty|amount|number)\b", t):
        return "quantity"
    if re.search(r"\b(product|item)\b", t):
        return "product"
    return "any"


def start_edit(ctx: Ctx, kind: str):
    """Ask what to change. The order (draft) is kept, so nothing is lost."""
    draft = ctx.data.get("draft") or []
    if not draft:
        clear_order(ctx)
        return ctx.reply("No problem. Tell me the products and quantities you want, "
                         "for example: \"10 HP laptops and 2 chargers\".")
    if kind == "quantity" and len(draft) == 1:
        p = ctx.db.get(Product, draft[0]["product_id"])
        return ask_quantity(ctx, p, f"How many {p.name} would you like? We have {p.stock:,} available.",
                            from_queue=False)
    if kind == "product":
        question = "Which product would you like instead? For example: \"HP ProBook\" or \"3 Lenovo laptops\"."
    else:
        question = ("What would you like to change? Send a new quantity (for example: 5) "
                    "or a different product (for example: 3 HP ProBook).")
    ctx.stage("awaiting_edit")
    ctx.data["last_question"] = question
    return ctx.reply(question)


def edit_quantity(ctx: Ctx, n: int):
    """A plain number at the summary: the new quantity of the (only) product. Stock is checked again."""
    draft = ctx.data.get("draft") or []
    if len(draft) != 1:
        ctx.stage("awaiting_edit")
        return ctx.reply("Your order has more than one product. Please tell me the product and the new "
                         "quantity, for example: \"5 HP ProBook\".")
    if n <= 0:
        return ctx.reply("Please send a quantity of 1 or more.")
    draft[0]["quantity"] = n
    return validate_and_summarize(ctx)


def edits_current_order(ctx: Ctx, items: list) -> bool:
    """True if a product message at the summary changes this order ('make it the ProBook instead',
    'I need 6 Dell laptops' for the Dell already in the order). Otherwise it is a new order."""
    if INSTEAD_RE.search(ctx.text.lower()):
        return True
    ids = {d["product_id"] for d in ctx.data.get("draft") or []}
    products = ctx.db.query(Product).all()
    for it in items:
        chosen, _ = choose_product(ctx, it["product_text"], products)
        if not chosen or chosen.id not in ids:
            return False
    return True


def apply_edit(ctx: Ctx, items: list):
    """Change the order: a product already in it gets the new quantity; a different product replaces the
    only product (keeping its quantity unless a new one is given), or is added with 'add / also'.
    Then the usual product search -> stock check -> new summary."""
    products = ctx.db.query(Product).all()
    resolved = [(it, *choose_product(ctx, it["product_text"], products)) for it in items]
    for it, chosen, options in resolved:
        if not chosen and not options:
            ctx.reply(f"Sorry, I couldn't find \"{it['product_text']}\" in our catalogue, so your order is unchanged.")
            return validate_and_summarize(ctx)
    draft = ctx.data.setdefault("draft", [])
    adding = bool(ADD_RE.search(ctx.text.lower()))
    new_items = []
    for it, chosen, options in resolved:
        if chosen and any(d["product_id"] == chosen.id for d in draft):
            if it.get("quantity"):
                add_to_draft(ctx, chosen.id, int(it["quantity"]), replace=not adding)
            continue
        new_items.append(dict(it))
    if new_items and not adding and len(draft) == 1 and len(new_items) == 1:
        old = draft.pop()                          # product change
        if not new_items[0].get("quantity"):
            new_items[0]["quantity"] = old["quantity"]
    ctx.data["queue"] = new_items
    return process_queue(ctx)


# ------------------------------------------------------------------ offer the salesperson (customer decides)
def offer_salesperson(ctx: Ctx, reason: str, question: str, lines=None):
    """Ask before handing off. 'Yes' -> the salesperson gets the order details; 'No' -> carry on."""
    ctx.data["pending_offer"] = reason
    if lines is not None:
        ctx.data["offer_lines"] = lines
    return ctx.reply(question, {"type": "offer", "reason": reason})


def decline_offer(ctx: Ctx):
    stage = ctx.state.stage
    if stage == "awaiting_confirmation" and ctx.data.get("draft"):
        ctx.reply("No problem. Here is your order again:")
        return validate_and_summarize(ctx)
    if stage in {"awaiting_quantity", "awaiting_choice", "awaiting_edit"} and ctx.data.get("last_question"):
        return ctx.reply("No problem. " + ctx.data["last_question"])
    return ctx.reply("No problem. How else can I help you today?")


def not_understood(ctx: Ctx):
    ctx.data["tries"] = ctx.data.get("tries", 0) + 1
    if ctx.data["tries"] >= config.MAX_CLARIFY_TRIES:
        return escalate(ctx, "not_understood")
    if ctx.state.stage == "awaiting_choice":
        return ctx.reply("Please reply with the number of the product you want (for example: 1).")
    if ctx.state.stage == "awaiting_quantity":
        return ctx.reply("Please reply with the quantity as a number (for example: 10).")
    # Ask once more before calling the salesperson (one unclear message is not a reason to hand off).
    if ctx.state.stage == "awaiting_confirmation":
        return ctx.reply("Please reply 1 (Yes, Place Order), 2 (Edit Order) or 3 (Cancel Order).")
    if ctx.state.stage == "awaiting_edit":
        return ctx.reply("Please send a new quantity (for example: 5) or a product "
                         "(for example: 3 HP ProBook).")
    return ctx.reply("Sorry, I didn't understand that. Please tell me the product and how many you want, "
                     "for example: \"10 HP laptops\".")


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


def category_words(text: str, products: list) -> set:
    """Product categories named in the text, using the categories stored in the database
    ('laptops', 'notebooks' -> {'laptop'})."""
    by_cat = {product_search.normalize(p.category or "") for p in products if p.category}
    words = set(_tokens(text))
    return {w for w in words if w in by_cat} | {CATEGORY_SYNONYMS[w] for w in words
                                                if CATEGORY_SYNONYMS.get(w) in by_cat}


def brand_words(products: list) -> set:
    """All brands stored in the database ('hp', 'dell', ...)."""
    return {product_search.normalize(p.brand) for p in products if p.brand}


def names_a_model(text: str, products: list) -> bool:
    """True if the customer named a specific model ('hp 250', 'probook', 'hp 15', 'v15'),
    not just a brand and a product type ('hp laptops', 'dell notebooks')."""
    brands = brand_words(products)
    if specific_words(text, products) - brands:
        return True
    plain = brands | GENERIC_WORDS | product_search.STOP_WORDS
    q = " " + product_search.normalize(text) + " "
    for p in products:
        for nm in [p.name] + (p.nicknames or "").split(","):
            nm = product_search.normalize(nm).strip()
            if nm and f" {nm} " in q and any(w not in plain for w in nm.split()):
                return True          # e.g. the nickname "hp 15" (has the model number 15)
    return False


def identify(text: str, products: list) -> dict:
    """What the customer's product words mean, checked against the database."""
    return {"brand": sorted(specific_words(text, products) & brand_words(products)) or None,
            "category": sorted(category_words(text, products)) or None,
            "model_named": names_a_model(text, products)}


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
    # Brand + product type only ("4 HP laptops", "HP notebooks"): several models of that brand can fit,
    # so the customer chooses, just like for "10 laptops". A model name ("hp 250", "probook") still
    # picks one product as before. The list comes from the same Hybrid Search results.
    cats = category_words(product_text, products)
    if cats and not names_a_model(product_text, products):
        in_cat = [(p, s) for p, s in options if product_search.normalize(p.category or "") in cats]
        if len(in_cat) == 1:
            return in_cat[0][0], in_cat
        if in_cat:
            return None, in_cat
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
        who = identify(item["product_text"], products)
        print(f"🧠 understood: {item['product_text']!r} | quantity={item.get('quantity')} | brand={who['brand']} | "
              f"category={who['category']} | model named={who['model_named']} -> "
              + (f"picked {chosen.name}" if chosen else f"{len(options)} option(s): " + ", ".join(p.name for p, _ in options)))
        if not chosen:
            options = options[:10]
            if not options:
                queue.pop(0)
                return product_not_found(ctx, item["product_text"], products)
            ctx.data["clarified"] = True
            ctx.data["choices"] = [p.id for p, _ in options]
            ctx.stage("awaiting_choice")
            qty = item.get("quantity")
            # price comes from the database
            rows = []
            for i, (p, _) in enumerate(options):
                row = (f"{i + 1}. {p.name} – {naira(p.price)} per {unit(p)} – "
                       + (f"{p.stock:,} in stock" if p.stock > 0 else "out of stock"))
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
            return ask_quantity(ctx, chosen, f"How many {chosen.name} do you want?", from_queue=True)
        add_to_draft(ctx, chosen.id, int(item["quantity"]))
        queue.pop(0)
    return validate_and_summarize(ctx)


def product_not_found(ctx: Ctx, words: str, products: list):
    """Nothing in the catalogue matches. If this was the only thing asked for, say what we sell and ask
    again; the salesperson is called on the next miss (MAX_CLARIFY_TRIES). If it was part of a bigger
    order, hand off as before, so the rest of the order is not lost."""
    if ctx.data.get("draft") or ctx.data.get("queue"):
        return escalate(ctx, "unknown_product")
    tries = ctx.data.get("tries", 0) + 1
    if tries >= config.MAX_CLARIFY_TRIES:
        return escalate(ctx, "unknown_product")
    clear_order(ctx)
    ctx.data["tries"] = tries                  # clear_order resets it; keep this miss counted
    kinds = ", ".join(sorted({p.category for p in products if p.category}))
    return ctx.reply(f"Sorry, I couldn't find \"{words.strip()}\" in our catalogue. We sell: {kinds}. "
                     "What would you like? For example: \"10 HP laptops\".")


def product_enquiry(ctx: Ctx):
    """Customer asks what we have ('what laptops do you have'): list ALL matching products from the DB."""
    products = ctx.db.query(Product).order_by(Product.id).all()
    by_cat = {product_search.normalize(p.category or ""): p.category for p in products if p.category}
    cats = category_words(ctx.text, products)
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
        ctx.data["draft"] = []          # a question, not part of an order being built
        return product_not_found(ctx, ctx.text, products)

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
        return ask_quantity(ctx, p, f"Great. How many {p.name} do you want?", from_queue=True)
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
    ctx.data.update({"pending_product": None, "pending_product_name": None, "available_stock": None,
                     "requested_quantity": None, "last_question": None})
    return process_queue(ctx)


# ------------------------------------------------------------------ STEP 4 + 5: validate, summary
def order_block(ctx: Ctx, lines: list, total: float):
    """None = the bot may place the order; 'big_order' = it needs the salesperson's approval first.
    A salesperson-approved order ('approved') is not blocked.
    Available credit is NOT checked here: orders are placed even when they are above the customer's credit."""
    if ctx.data.get("approved"):
        return None
    if total >= config.BIG_ORDER_AMOUNT or any(l["quantity"] >= config.BIG_ORDER_QTY for l in lines):
        return "big_order"
    return None


def validate_and_summarize(ctx: Ctx):
    draft = ctx.data.get("draft") or []
    notes, gone = [], []
    for d in list(draft):
        p = ctx.db.get(Product, d["product_id"])
        if p.stock <= 0:
            draft.remove(d)
            gone.append(d)
            notes.append(f"Sorry, {p.name} is out of stock right now.")
        elif p.stock < d["quantity"]:
            return ask_quantity(ctx, p, f"Only {p.stock:,} units of {p.name} are available. "
                                        f"Would you like to change your quantity or speak with a salesperson?",
                                from_queue=False, requested=d["quantity"])
    if not draft:
        ctx.stage("idle")
        return offer_salesperson(ctx, "out_of_stock", " ".join(notes) + " Would you like me to connect you with "
                                 "a salesperson? They can tell you when it will be available.", lines=gone)

    # QUOTATION: price and stock from the database. Credit is NOT checked here; it is checked when the
    # customer says YES (place_order), so a quotation is always shown when the product is in stock.
    discount = ctx.data.get("discount_percent", 0) or 0
    lines, total = draft_lines(ctx.db, draft, discount)

    ctx.stage("awaiting_confirmation")
    ctx.data["tries"] = 0
    text_lines = ["Quotation", ""]
    for l in lines:
        text_lines += [f"Product: {l['name']}",
                       f"Quantity: {l['quantity']}",
                       f"Unit price: {naira(l['unit_price'])}",
                       f"Available stock: {l['stock']:,}",
                       ""]
    text_lines += [f"Quotation total: {naira(total)}" + (f" (after {discount:g}% discount)" if discount else ""),
                   "",
                   "Your quotation is ready. Would you like to proceed with this order?",
                   "1. Yes, Place Order",
                   "2. Edit Order",
                   "3. Cancel Order"]
    msg = "\n".join(notes + text_lines)
    return ctx.reply(msg, {"type": "order_summary", "items": lines, "total": total, "discount_percent": discount})


# ------------------------------------------------------------------ STEP 6 + 7: place order, feedback
def place_order(ctx: Ctx):
    draft = ctx.data.get("draft") or []
    # Re-check stock at the last moment. The product rows are locked (PostgreSQL SELECT ... FOR UPDATE)
    # until this order is saved, so two customers can never both buy the last units.
    ids = [d["product_id"] for d in draft]
    locked = {p.id: p for p in ctx.db.query(Product).filter(Product.id.in_(ids))
              .populate_existing().with_for_update().all()}
    for d in draft:
        p = locked.get(d["product_id"])
        if p is None or p.stock < d["quantity"]:
            return validate_and_summarize(ctx)
    discount = ctx.data.get("discount_percent", 0) or 0
    lines, total = draft_lines(ctx.db, draft, discount)
    # Fresh, locked copy of the customer, so the credit_used bookkeeping below is updated safely
    # (credit is NOT checked: the order is placed even when it is above the available credit).
    ctx.customer = (ctx.db.query(Customer).filter_by(id=ctx.customer.id)
                    .populate_existing().with_for_update().one())
    block = order_block(ctx, lines, total)
    if block:                                  # needs approval first: NOT saved, NOT confirmed
        return request_approval(ctx, block, lines, total)
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
    # The confirmation is built from the SAVED rows, and it is saved in the SAME database transaction as the
    # order, the order items and the stock/credit updates: it can only exist if the order was saved.
    ctx.db.flush()
    saved = ctx.db.query(Order).filter_by(id=order.id).one()
    rows = []
    for it in ctx.db.query(OrderItem).filter_by(order_id=saved.id).order_by(OrderItem.id).all():
        rows += [f"- Product: {it.product.name}", f"- Quantity: {it.quantity}", f"- Unit Price: {naira(it.unit_price)}"]
    return ctx.reply("\n".join(["✅ Order Placed Successfully!", "",
                                f"Order ID: {saved.order_code}", "",
                                "Order Summary"] + rows +
                               [f"- Total Amount: {naira(saved.total)}", "",
                                f"Order Status: {saved.status.capitalize()}", "",
                                "Thank you for your order! Your order has been successfully placed."]),
                     {"type": "order_placed", "order_code": saved.order_code, "total": saved.total})


def request_approval(ctx: Ctx, block: str, lines: list, total: float):
    """The customer said YES, but the order is a big order (BIG_ORDER_QTY / BIG_ORDER_AMOUNT), which needs
    the salesperson's approval. The order is NOT saved. The quotation goes to the salesperson; when they
    approve it ("hand back to AI with order summary"), the customer gets the quotation again and YES places it."""
    sp = ctx.customer.salesperson
    ctx.db.add(Alert(kind="handoff", customer_phone=ctx.phone, salesperson_id=sp.id if sp else None,
                     reason=block, message=ctx.text, status="open",
                     details=json.dumps({"draft": lines, "total": total, "approval_needed": True})))
    ctx.stage("awaiting_approval")             # a second YES does not send another request
    why = (f"orders of {config.BIG_ORDER_QTY:,} or more of one product, or {naira(config.BIG_ORDER_AMOUNT)} or "
           f"more in total, need approval")
    who = f"your salesperson, {sp.name}," if sp else "our salesperson"
    return ctx.reply(f"Your order needs approval before it can be placed: {why}.\n"
                     f"I've sent your quotation to {who} for approval.\n"
                     "Status: Pending approval. Your order is NOT placed yet.\n"
                     "Once it is approved, I'll send you the quotation again to confirm. "
                     "You can also reply EDIT to change it or CANCEL to cancel it.",
                     {"type": "approval_pending", "reason": block, "total": total})


def withdraw_approval(ctx: Ctx):
    """The customer edits or cancels while approval is pending: close that approval request."""
    for a in ctx.db.query(Alert).filter(Alert.customer_phone == ctx.phone, Alert.kind == "handoff",
                                        Alert.status == "open", Alert.reason.in_(["credit", "big_order"])).all():
        a.status = "resolved"


def clear_order(ctx: Ctx):
    ctx.data.update({"draft": [], "queue": [], "choices": [], "pending_product": None, "tries": 0,
                     "approved": False, "discount_percent": 0, "clarified": False,
                     "pending_product_name": None, "available_stock": None, "requested_quantity": None,
                     "last_question": None, "pending_offer": None, "offer_lines": None})
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


def escalate(ctx: Ctx, reason: str, salesperson=None, accepted: bool = False, reply: str = None):
    """Save an alert for the salesperson, with the order details, and tell the customer.
    accepted=True: the customer asked for / agreed to a salesperson. reply: a fixed text for the customer.
    The chat is NOT locked."""
    sp = salesperson or (ctx.customer.salesperson if ctx.customer else None)
    draft = ctx.data.get("draft") or []
    lines, total = draft_lines(ctx.db, draft or ctx.data.get("offer_lines") or [], 0)
    ctx.db.add(Alert(kind="handoff", customer_phone=ctx.phone, salesperson_id=sp.id if sp else None,
                     reason=reason, message=ctx.text, status="open",
                     details=json.dumps({"draft": lines, "total": total,
                                         "lead_details": ctx.data.get("lead_details")})))
    text = HANDOFF_REPLY
    if accepted:
        who = f"your salesperson, {sp.name}," if sp else "our salesperson,"
        text = (f"Okay! I've passed your order details to {who} who will contact you shortly. 🙏" if lines
                else f"Okay! I've asked {who} to contact you shortly. 🙏")
    if reply:
        text = reply
    clear_order(ctx)   # stage is idle: nothing is waiting for YES, so no order can be placed by mistake
    ctx.data["draft"] = draft   # kept for the salesperson's "hand back to AI with order summary"
    ctx.reply(text, {"type": "handoff", "reason": reason})


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
