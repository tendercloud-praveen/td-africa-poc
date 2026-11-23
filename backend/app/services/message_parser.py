"""
Rule-based understanding of customer messages (used when Azure OpenAI is not set,
and as a safety fallback when it fails).

Output shape (same as the AI version):
{
  "intent": "order" | "enquiry" | "confirm" | "change" | "cancel" | "status" | "reorder" | "greeting" | "other",
  "items": [{"product_text": str, "quantity": int | None}],
  "wants_discount": bool,
  "wants_human": bool,
  "choice_number": int | None
}
"""
import re

NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "hundred": 100, "dozen": 12,
}
YES = {"yes", "y", "yeah", "yep", "yea", "ok", "okay", "k", "confirm", "confirmed", "sure", "go ahead",
       "oya", "na so", "correct", "yes please", "yes pls", "ok go ahead", "proceed", "e correct", "done"}
CHANGE = {"change", "modify", "edit", "no", "nope", "not correct", "wrong", "change am"}
CANCEL = ("cancel", "forget it", "no need", "leave am", "stop order")
HUMAN = ("talk to", "speak to", "speak with", "talk with", "a person", "human", "real person",
         "agent", "salesperson", "sales person", "call me", "customer care")
DISCOUNT = ("discount", "reduce price", "reduce the price", "reduce am", "cheaper", "last price",
            "best price", "rebate", "price reduction", "% off", "percent off", "can you do", "lower price",
            "offers", "any offer", "special offer", "special price", "special pricing", "promo", "deals")
STATUS = ("status", "where is my order", "where my order", "track", "my order reach", "delivery date",
          "when will", "has my order")
REORDER = ("same as last", "same thing as last", "usual", "last order", "same order", "repeat order",
           "same as before", "same one as before", "like last time")
# "what laptops do you have", "which printers do you provide", "show me laptops", "price of laptops"
ENQUIRY = re.compile(
    r"\b(what|which|wetin|show|list|available|provide|sell|selling|do you have|you have|have you got|"
    r"got any|price of|prices|price list|how much|catalog|catalogue|options|una get|una dey sell)\b")
GREETING = ("hi", "hello", "hey", "good morning", "good afternoon", "good evening", "morning",
            "how far", "how you dey", "hiya")


def _squeeze(text):
    """'hiii' -> 'hi', 'helloo' -> 'helo' (compare greetings without stretched letters)."""
    return re.sub(r"(.)\1+", r"\1", text)


GREETING_EXTRA = ("sir", "ma", "there", "team", "td africa", "oga", "boss", "o", "dear")


def is_only_greeting(t: str) -> bool:
    """True when the whole message is just a greeting: 'hi', 'hii', 'Hello sir!', 'good morning 👋'."""
    words = re.sub(r"[^a-z ]+", " ", t.lower()).split()
    if not words:
        return False
    rest = _squeeze(" ".join(words))
    for g in sorted({_squeeze(x) for x in GREETING}, key=len, reverse=True):
        if rest == g or rest.startswith(g + " "):
            rest = rest[len(g):].strip()
            break
    else:
        return False
    extras = {_squeeze(x) for x in GREETING_EXTRA}
    while rest:
        for e in sorted(extras, key=len, reverse=True):
            if rest == e or rest.startswith(e + " "):
                rest = rest[len(e):].strip()
                break
        else:
            return False
    return True


def _clean(text):
    return re.sub(r"\s+", " ", (text or "").lower().strip(" .!?"))


def split_items(text):
    """'10 hp laptops and 2 chargers' -> [('hp laptops', 10), ('chargers', 2)]"""
    text = _clean(text)
    chunks = re.split(r"\s*(?:,|\+|&|;|\.\s|\band\b|\bplus\b|\balso\b|\n)\s*", text)
    items = []
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        qty, rest = _pick_quantity(ch)
        product_text = _trim_filler(re.sub(r"\s+", " ", rest).strip())
        if _is_only_filler(product_text):
            continue
        items.append({"product_text": product_text, "quantity": qty})
    return items


def _pick_quantity(chunk):
    """Prefer a number that looks like a quantity (at the start, or with x/pcs/units)."""
    # number at the very start: "10 hp laptops"
    m = re.match(r"^(?:i want |i need |send |give me |abeg |pls |please )*(\d{1,6})\b\s*(?:x|pcs|pc|units?|pieces?)?\s+(.*)$", chunk)
    if m:
        return int(m.group(1)), m.group(2)
    # explicit marker anywhere: "hp laptop x10", "hp laptop 10pcs", "hp laptop qty 10"
    m = re.search(r"(?:\bx\s*(\d{1,6})\b|\b(\d{1,6})\s*(?:x|pcs|pc|units?|pieces?)\b|\bqty\s*:?\s*(\d{1,6})\b)", chunk)
    if m:
        n = next(g for g in m.groups() if g)
        return int(n), chunk[:m.start()] + " " + chunk[m.end():]
    # "i want 10 hp laptops" (number after filler words)
    m = re.search(r"\b(?:want|need|send|send me|give me|order|buy|get|abeg|pls|please)\s+(\d{1,6})\b", chunk)
    if m:
        return int(m.group(1)), chunk[:m.start(1)] + " " + chunk[m.end(1):]
    # a lone number at the end: "hp laptop 10" (only if 1-4 digits and not glued to letters)
    m = re.search(r"\s(\d{1,4})$", chunk)
    if m and not re.search(r"(gb|tb|w|va|mah)$", chunk[:m.start()].strip()):
        return int(m.group(1)), chunk[:m.start()]
    for word, n in NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", chunk):
            return n, re.sub(rf"\b{word}\b", " ", chunk, count=1)
    return None, chunk


FILLER = {"i", "want", "need", "send", "me", "pls", "please", "abeg", "give", "order", "some", "the", "a",
          "of", "units", "unit", "pcs", "and", "also", "more", "hi", "hello", "good", "morning", "sir", "ma",
          "oga", "to", "for", "my", "we", "us", "buy", "get", "like", "would", "can", "you", "x", "qty",
          "make", "una", "dey", "kindly", "need", "wan", "plz"}


def _trim_filler(text):
    words = text.split()
    while words and words[0] in FILLER:
        words.pop(0)
    while words and words[-1] in FILLER:
        words.pop()
    return " ".join(words)


def _is_only_filler(text):
    words = re.findall(r"[a-z0-9]+", text)
    return not words or all(w in FILLER for w in words)


def lone_number(text):
    m = re.fullmatch(r"\s*(?:option|number|no\.?|#)?\s*(\d{1,6})\s*", (text or "").lower())
    return int(m.group(1)) if m else None


def parse(text: str, stage: str = "idle") -> dict:
    t = _clean(text)
    result = {"intent": "other", "items": [], "wants_discount": False, "wants_human": False,
              "choice_number": lone_number(t)}

    result["wants_human"] = any(h in t for h in HUMAN)
    result["wants_discount"] = any(d in t for d in DISCOUNT) or bool(re.search(r"\d+\s*%", t))

    if t in YES:
        result["intent"] = "confirm"
        return result
    if is_only_greeting(t):
        result["intent"] = "greeting"
        return result
    if t in CHANGE:
        result["intent"] = "change"
        return result
    if any(c in t for c in CANCEL):
        result["intent"] = "cancel"
        return result
    if any(s in t for s in STATUS):
        result["intent"] = "status"
        return result
    if any(r in t for r in REORDER):
        result["intent"] = "reorder"
        return result
    if result["choice_number"] is not None:
        result["intent"] = "number"
        return result

    # strip a greeting at the start ("good morning, I want ...")
    body = t
    for g in sorted(GREETING, key=len, reverse=True):
        if body.startswith(g):
            body = body[len(g):].strip(" ,.!")
            break
    if not body:
        result["intent"] = "greeting"
        return result

    if result["wants_human"] and len(body.split()) <= 8 and not re.search(r"\d", body):
        return result
    # Product enquiry: asking what we have (no quantity given) -> list, don't order
    if ENQUIRY.search(body) and not any(it["quantity"] for it in split_items(body)):
        result["intent"] = "enquiry"
        return result
    if result["wants_discount"]:
        body = re.sub(r"\d+\s*%|(give me|with)?\s*(a\s+)?discount.*$", " ", body)

    items = split_items(body)
    if items:
        result["intent"] = "order"
        result["items"] = items
    return result
