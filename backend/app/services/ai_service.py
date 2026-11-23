"""
AI helpers. Works with:
  - Ollama (free, local)  -> LLM_PROVIDER=ollama  (uses Ollama's OpenAI-compatible API at /v1)
  - Azure OpenAI          -> LLM_PROVIDER=azure
  - nothing               -> LLM_PROVIDER=none, or when Ollama is not running

The AI is allowed to do two things:
  1. Understand the customer's message (intent, products, quantities).
  2. Reason over the RETRIEVED product list and pick which one the customer means.
It never sees or decides prices, stock or credit.
Every AI call has a safe fallback to the local rules, so the demo never breaks.
"""
import json
import re
import urllib.request

from app import config
from app.services import message_parser

_client = None


def _get_client():
    global _client
    if _client is None:
        if config.LLM_PROVIDER == "azure":
            from openai import AzureOpenAI
            _client = AzureOpenAI(
                azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                api_key=config.AZURE_OPENAI_API_KEY,
                api_version=config.AZURE_OPENAI_API_VERSION,
                timeout=config.AI_TIMEOUT,
                max_retries=1,
            )
        else:
            from openai import OpenAI
            _client = OpenAI(
                base_url=config.OLLAMA_BASE_URL + "/v1",
                api_key="ollama",            # Ollama ignores the key, but the client needs one
                timeout=config.AI_TIMEOUT,
                max_retries=0,               # a retry on a slow CPU would double the wait
            )
    return _client


def chat_model() -> str:
    return config.AZURE_OPENAI_DEPLOYMENT if config.LLM_PROVIDER == "azure" else config.OLLAMA_CHAT_MODEL


def embed_model() -> str:
    return config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT if config.LLM_PROVIDER == "azure" else config.OLLAMA_EMBED_MODEL


def embedding_id() -> str:
    """Stored next to each product vector, so changing the model re-computes them."""
    return f"{config.LLM_PROVIDER}:{embed_model()}"


# ------------------------------------------------------------------ startup check
def _model_present(wanted: str, names: set) -> bool:
    w = wanted if ":" in wanted else wanted + ":latest"
    return w in names or wanted in names


def check_ai() -> str:
    """Called once at startup. Decides what is ON, prints it, and returns the label."""
    if config.LLM_PROVIDER == "none":
        config.AI_ENABLED = config.EMBEDDINGS_ENABLED = False
        config.AI_MODE_LABEL = "Local demo mode (LLM_PROVIDER=none)"
        print(f"🤖 AI mode: {config.AI_MODE_LABEL}")
        return config.AI_MODE_LABEL

    if config.LLM_PROVIDER == "azure":
        if config.AI_ENABLED:
            config.AI_MODE_LABEL = "Azure OpenAI"
            print(f"🤖 AI mode: Azure OpenAI ({config.AZURE_OPENAI_DEPLOYMENT})"
                  + (f", embeddings: {config.AZURE_OPENAI_EMBEDDING_DEPLOYMENT}" if config.EMBEDDINGS_ENABLED else ""))
        else:
            config.AI_MODE_LABEL = "Local demo mode (Azure keys missing)"
            print("⚠️  LLM_PROVIDER=azure but AZURE_OPENAI_* is not filled in .env → using local rules")
        return config.AI_MODE_LABEL

    # ---- Ollama: is it running, and are both models pulled?
    try:
        with urllib.request.urlopen(config.OLLAMA_BASE_URL + "/api/tags", timeout=5) as r:
            tags = json.loads(r.read().decode("utf-8"))
        names = {m.get("name") for m in tags.get("models", [])} | {m.get("model") for m in tags.get("models", [])}
    except Exception as e:  # noqa: BLE001
        config.AI_ENABLED = config.EMBEDDINGS_ENABLED = False
        config.AI_MODE_LABEL = "Local demo mode (Ollama not running)"
        print(f"⚠️  Ollama is not reachable at {config.OLLAMA_BASE_URL} ({e.__class__.__name__}).")
        print("   Start the Ollama app (or run 'ollama serve'), then restart the backend.")
        print(f"🤖 AI mode: {config.AI_MODE_LABEL} → using local rules + local search")
        return config.AI_MODE_LABEL

    config.AI_ENABLED = _model_present(config.OLLAMA_CHAT_MODEL, names)
    config.EMBEDDINGS_ENABLED = _model_present(config.OLLAMA_EMBED_MODEL, names)
    if not config.AI_ENABLED:
        print(f"⚠️  Ollama chat model '{config.OLLAMA_CHAT_MODEL}' is not pulled. Run: ollama pull {config.OLLAMA_CHAT_MODEL}")
    if not config.EMBEDDINGS_ENABLED:
        print(f"⚠️  Ollama embedding model '{config.OLLAMA_EMBED_MODEL}' is not pulled. Run: ollama pull {config.OLLAMA_EMBED_MODEL}")

    if config.AI_ENABLED:
        config.AI_MODE_LABEL = f"Ollama ({config.OLLAMA_CHAT_MODEL})"
    else:
        config.AI_MODE_LABEL = "Local demo mode (Ollama model missing)"
    print(f"🤖 AI mode: {config.AI_MODE_LABEL}")
    print(f"🔎 Embeddings: {'Ollama (' + config.OLLAMA_EMBED_MODEL + ')' if config.EMBEDDINGS_ENABLED else 'off (local fuzzy search only)'}")
    return config.AI_MODE_LABEL


def warm_up():
    """Load the chat model into memory so the first customer message is not extra slow."""
    if not (config.AI_ENABLED and config.LLM_PROVIDER == "ollama"):
        return
    try:
        _get_client().chat.completions.create(
            model=chat_model(), messages=[{"role": "user", "content": "hi"}], max_tokens=1)
        print("🔥 Ollama chat model loaded and ready")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  Ollama warm-up failed: {e}")


# ------------------------------------------------------------------ JSON chat
def _first_json_object(text: str) -> dict:
    """Small models sometimes wrap JSON in text or ``` fences. Pull out the first {...} block."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                        if isinstance(data, dict):
                            return data
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    raise ValueError(f"No JSON object in AI reply: {text[:120]!r}")


def _chat_json(system: str, user: str) -> dict:
    resp = _get_client().chat.completions.create(
        model=chat_model(),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return _first_json_object(resp.choices[0].message.content or "")


# ------------------------------------------------------------------ 1. understand the message
EXTRACT_PROMPT = """You read WhatsApp messages sent by Nigerian business customers to TD Africa, an IT products distributor.
Messages may be in Nigerian English, Pidgin, or mix in Yoruba, Igbo or Hausa words, with typos and abbreviations.

Return ONLY a JSON object with these keys:
{"intent": "order|enquiry|confirm|change|cancel|status|reorder|greeting|number|other",
 "items": [{"product_text": "<product words>", "quantity": <integer or null>}],
 "wants_discount": true|false,
 "wants_human": true|false,
 "choice_number": <integer or null>}

Rules:
- "order" = they list products to buy. "confirm" = they agree (yes, ok, go ahead, oya, na so).
- "enquiry" = they ask what products or prices we have, without giving a quantity
  ("what laptops do you have", "which printers do you provide", "show me laptops", "price of laptops").
- "change" = edit the order. "cancel" = cancel. "status" = where is my order. "reorder" = same as last time.
- "number" = the whole message is only a number (then put it in choice_number).
- product_text = only the product words, without the quantity and without greetings.
- Numbers that are part of a model name stay in product_text ("hp 15", "64gb", "m185").
  "x5", "5pcs", "5 units" are quantities.
- wants_discount = true if they ask for a discount, a lower or special price, or a % off.
- wants_human = true if they ask for a person, salesperson or agent.
- Do not invent products or quantities. Never output prices.

Examples:
"Good morning, I want 10 HP laptops" -> {"intent":"order","items":[{"product_text":"hp laptops","quantity":10}],"wants_discount":false,"wants_human":false,"choice_number":null}
"hp 15 x5 and 2 chargers" -> {"intent":"order","items":[{"product_text":"hp 15","quantity":5},{"product_text":"chargers","quantity":2}],"wants_discount":false,"wants_human":false,"choice_number":null}
"abeg send 3 epson printer, reduce price small" -> {"intent":"order","items":[{"product_text":"epson printer","quantity":3}],"wants_discount":true,"wants_human":false,"choice_number":null}
"2" -> {"intent":"number","items":[],"wants_discount":false,"wants_human":false,"choice_number":2}"""

INTENTS = {"order", "enquiry", "confirm", "change", "cancel", "status", "reorder", "greeting", "number", "other"}
HUMAN_HINT = re.compile(r"\b(person|human|people|agent|someone|somebody|sales\s*person|salesman|staff|manager|"
                        r"oga|talk|speak|call|representative|customer\s*care|support)\b", re.I)
NEGATIVE = re.compile(r"\b(no|not|don'?t|dont|wait|change|cancel|stop|never)\b", re.I)


def _to_int(v):
    try:
        n = int(float(v))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _numbers_in(text: str) -> set:
    nums = {int(n) for n in re.findall(r"\d+", text or "")}
    low = (text or "").lower()
    nums |= {n for w, n in message_parser.NUMBER_WORDS.items() if re.search(rf"\b{w}\b", low)}
    return nums


def _words_in(product_text: str, message: str) -> bool:
    """At least one product word (or its start) must really be in the message — no invented products."""
    msg = message.lower()
    for w in re.findall(r"[a-z0-9]+", product_text.lower()):
        if len(w) >= 2 and (w in msg or (len(w) > 4 and w[:4] in msg)):
            return True
    return False


def extract(text: str, stage: str) -> dict:
    """AI understanding with a safe fallback to the rule-based parser."""
    fallback = message_parser.parse(text, stage)
    if not config.AI_ENABLED:
        return fallback
    try:
        data = _chat_json(EXTRACT_PROMPT, f"Conversation stage: {stage}\nCustomer message: {text}")

        msg_numbers = _numbers_in(text)
        items = []
        for it in data.get("items") or []:
            if not isinstance(it, dict):
                continue
            pt = str(it.get("product_text") or "").strip()
            if not pt or not _words_in(pt, text):
                continue
            q = _to_int(it.get("quantity"))
            if q is not None and q not in msg_numbers:
                q = None   # the AI made up a number that is not in the message
            if q is None:
                # Small models are not consistent ("40 probook" -> "probook 40", no quantity).
                # If the rules found a quantity for the same product, use it.
                ai_words = set(re.findall(r"[a-z]+", pt.lower()))
                for fb in fallback["items"]:
                    if fb.get("quantity") and ai_words & set(re.findall(r"[a-z]+", fb["product_text"].lower())):
                        q = fb["quantity"]
                        if str(q) in pt.split():
                            pt = fb["product_text"]
                        break
            items.append({"product_text": pt, "quantity": q})

        intent = str(data.get("intent") or "other").lower()
        if intent not in INTENTS:
            intent = "other"

        # Safety: the rule parser is exact for short replies, so trust it for these.
        if fallback["intent"] in {"confirm", "change", "cancel", "number", "status", "reorder", "greeting", "enquiry"}:
            intent = fallback["intent"]
        # an "enquiry" that gives a quantity is really an order
        if intent == "enquiry" and any(it["quantity"] for it in items):
            intent = "order"
        # The AI called it a question but the rules found a quantity ("please send me 3 lenovo laptops"):
        # it is an order, and the quantity must not be lost.
        if intent == "enquiry" and fallback["intent"] == "order" and any(it["quantity"] for it in fallback["items"]):
            intent, items = "order", fallback["items"]
        # "Is the whole message just a number?" is exact in the rules; the AI must not guess it
        # (e.g. "printer 4" is an order for 4 printers, not option 4).
        if intent == "number" and fallback["intent"] != "number":
            intent = "order" if (items or fallback["intent"] == "order") else "other"
        # Never treat a message as YES unless it really looks like a yes (an order is placed on YES).
        if intent == "confirm" and fallback["intent"] != "confirm" and NEGATIVE.search(text):
            intent = "other"
        # If the AI found nothing usable but the rules did, keep the rules' items.
        if intent == "order" and not items:
            items = fallback["items"]
            intent = "order" if items else "other"
        if intent in {"other", "greeting"} and fallback["intent"] == "order":
            intent, items = "order", fallback["items"]

        return {
            "intent": intent,
            "items": items,
            "wants_discount": bool(data.get("wants_discount") is True) or fallback["wants_discount"],
            # small models sometimes flag a plain "hi" as wanting a person: only believe it if the message hints at it
            "wants_human": (data.get("wants_human") is True and bool(HUMAN_HINT.search(text))) or fallback["wants_human"],
            "choice_number": fallback["choice_number"],   # exact rule: the message is only a number
        }
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  AI extract failed, using rule-based parser: {e}")
        return fallback


# ------------------------------------------------------------------ 2. RAG reasoning: pick a product
PICK_PROMPT = """You help match a customer's product words to ONE product from a short catalog list
that a search system retrieved. Use brand, category and nicknames. Nigerian shorthand is common
(e.g. "hp 15" can mean a 15-inch HP notebook).
Return ONLY JSON: {"product_id": <id from the list or null>, "confident": true|false}
Only choose a product if the customer named its brand or model (e.g. "hp 15", "dell laptop", "lenovo v15").
If they only used a general word (laptop, printer, mouse, charger), return null and confident=false.
Also return null if two or more products fit equally well, or none fit."""


def pick_product(customer_words: str, full_message: str, candidates: list):
    """RAG reasoning step. candidates = [(product, score)]. Returns (product or None, confident)."""
    if not config.AI_ENABLED or not candidates:
        return None, False
    try:
        listing = "\n".join(
            f"- id={p.id} | {p.name} | brand={p.brand} | category={p.category} | nicknames={p.nicknames}"
            for p, _ in candidates
        )
        data = _chat_json(PICK_PROMPT, f"Customer words: {customer_words}\nFull message: {full_message}\n\nCatalog:\n{listing}")
        pid = _to_int(data.get("product_id"))
        for p, _ in candidates:
            if pid is not None and pid == p.id:
                return p, data.get("confident") is True
        return None, False
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  AI pick failed, using search score only: {e}")
        return None, False


# ------------------------------------------------------------------ embeddings
def embed(texts: list, kind: str = "document") -> list:
    """kind = 'document' (a product) or 'query' (what the customer typed)."""
    if config.LLM_PROVIDER == "ollama" and config.OLLAMA_EMBED_MODEL.startswith("nomic-embed-text"):
        # nomic-embed-text works best with these task prefixes
        prefix = "search_query: " if kind == "query" else "search_document: "
        texts = [prefix + t for t in texts]
    resp = _get_client().embeddings.create(model=embed_model(), input=texts)
    return [d.embedding for d in resp.data]
