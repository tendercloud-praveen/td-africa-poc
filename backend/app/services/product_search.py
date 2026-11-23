"""
RAG retrieval step: find the catalog products that match what the customer typed.

- Always on: a local "fuzzy meaning" search (words + 3-letter pieces, TF-IDF).
  It handles typos ("lapptop"), plurals ("laptops") and nicknames ("hp 15").
- Semantic search: each product's embedding (Ollama nomic-embed-text, or Azure) is stored
  in the product_embeddings table and mixed in with cosine similarity. If a product has no
  embedding yet, or Ollama is off, the local search alone is used.

This step only FINDS products. Price and stock always come from the database
afterwards — never from the AI.
"""
import json
import math
import re
from collections import Counter

from app import config

STOP_WORDS = {
    "i", "we", "want", "need", "needs", "wan", "send", "me", "us", "pls", "plz", "please",
    "abeg", "give", "order", "of", "the", "a", "an", "some", "unit", "units", "pcs", "pc",
    "piece", "pieces", "for", "and", "to", "my", "our", "hi", "hello", "good", "morning",
    "afternoon", "evening", "sir", "ma", "oga", "make", "una", "dey", "you", "get", "buy",
    "can", "could", "would", "like", "kindly", "x", "more", "another", "with", "carton", "cartons",
}


def normalize(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = []
    for w in text.split():
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]  # laptops -> laptop
        words.append(w)
    return " ".join(words)


def _features(text: str) -> Counter:
    t = normalize(text)
    feats = Counter()
    for w in t.split():
        if w not in STOP_WORDS:
            feats["w:" + w] += 2
    joined = " " + " ".join(w for w in t.split() if w not in STOP_WORDS) + " "
    for i in range(len(joined) - 2):
        piece = joined[i:i + 3]
        if piece.strip():
            feats["c:" + piece] += 1
    return feats


def product_document(p) -> str:
    return " ".join(filter(None, [p.name, p.brand, p.category, (p.nicknames or "").replace(",", " ")]))


def _cosine(a: Counter, b: Counter, idf: dict) -> float:
    dot = sum(a[k] * idf.get(k, 1.0) * b.get(k, 0) * idf.get(k, 1.0) for k in a)
    na = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in a.items()))
    nb = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in b.items()))
    return dot / (na * nb) if na and nb else 0.0


def local_scores(products, query: str) -> list:
    docs = [_features(product_document(p)) for p in products]
    n = len(docs) or 1
    df = Counter()
    for d in docs:
        df.update(d.keys())
    idf = {k: math.log((n + 1) / (v + 1)) + 1 for k, v in df.items()}

    q = _features(query)
    q_norm = " " + normalize(query) + " "
    results = []
    for p, d in zip(products, docs):
        score = _cosine(q, d, idf)
        # Exact nickname or exact product name in the message = very strong match
        names = [normalize(x) for x in (p.nicknames or "").split(",") if x.strip()] + [normalize(p.name)]
        for nm in names:
            if nm and (" " + nm + " ") in q_norm:
                score = max(score, 0.9 if len(nm.split()) > 1 else 0.75)
        results.append((p, round(min(score, 1.0), 3)))
    return results


# ---------- embeddings stored in the database (Ollama or Azure) ----------
def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _stored_vector(p):
    """The product's saved vector, or None if missing / made by another model / product text changed."""
    from app.services.ai_service import embedding_id
    e = getattr(p, "embedding", None)
    if not e or e.model != embedding_id() or e.source_text != product_document(p):
        return None
    try:
        return json.loads(e.vector)
    except ValueError:
        return None


def ensure_embeddings(db, products=None, verbose=False) -> int:
    """Compute and save embeddings for products that have none, or whose text/model changed.
    Returns how many were (re)computed. Safe to call any time; does nothing if embeddings are off."""
    if not config.EMBEDDINGS_ENABLED:
        return 0
    from app.models.tables import Product, ProductEmbedding
    from app.services.ai_service import embed, embedding_id

    products = list(products) if products is not None else db.query(Product).all()
    todo = [p for p in products if _stored_vector(p) is None]
    if not todo:
        return 0
    if verbose:
        print(f"🔎 Computing embeddings for {len(todo)} product(s)...")
    done = 0
    for i in range(0, len(todo), 16):              # small batches: friendly to a laptop CPU
        batch = todo[i:i + 16]
        docs = [product_document(p) for p in batch]
        vectors = embed(docs, kind="document")
        for p, doc, vec in zip(batch, docs, vectors):
            e = p.embedding or ProductEmbedding(product_id=p.id)
            e.model, e.source_text, e.vector, e.dimensions = embedding_id(), doc, json.dumps(vec), len(vec)
            p.embedding = e
            done += 1
    db.commit()
    return done


def _embedding_scores(products, query: str) -> dict:
    """{product_id: 0..1} for products that have a stored vector. Others are simply left out."""
    from app.services.ai_service import embed
    vectors = {p.id: v for p in products if (v := _stored_vector(p)) is not None}
    if not vectors:
        return {}
    qv = embed([query], kind="query")[0]
    # rescale cosine to 0..1 (unrelated text is ~0.35-0.45 for nomic-embed-text, strong matches ~0.7+)
    lo, hi = (0.35, 0.75) if config.LLM_PROVIDER == "ollama" else (0.2, 0.8)
    return {pid: max(0.0, min(1.0, (_cos(qv, v) - lo) / (hi - lo))) for pid, v in vectors.items()}


def search(products, query: str, k: int = 5) -> list:
    """Return the top-k (product, score) pairs, best first. Score is 0..1.
    Hybrid = 50% local fuzzy score + 50% embedding similarity (when the product has a stored vector)."""
    products = list(products)
    if not products or not normalize(query).strip():
        return []
    results = local_scores(products, query)
    if config.EMBEDDINGS_ENABLED:
        try:
            emb = _embedding_scores(products, query)
            results = [(p, round(0.5 * s + 0.5 * emb[p.id], 3) if p.id in emb else s) for p, s in results]
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Embedding search failed, using local search only: {e}")
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:k]
