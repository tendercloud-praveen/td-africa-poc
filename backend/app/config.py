"""All settings come from the .env file."""
import os
from dotenv import load_dotenv

load_dotenv()


def _float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


# ---------- Database ----------
DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./td_africa_poc.db"
DB_ECHO = os.getenv("DB_ECHO", "false").lower() == "true"

# ---------- Business rules (change without touching code) ----------
BIG_ORDER_AMOUNT = _float("BIG_ORDER_AMOUNT", 20000000)   # ₦ total that needs a salesperson
BIG_ORDER_QTY = int(_float("BIG_ORDER_QTY", 100))          # quantity of one item that needs a salesperson
MAX_CLARIFY_TRIES = int(_float("MAX_CLARIFY_TRIES", 2))    # after this, hand over to salesperson

# ---------- AI provider: ollama (free, local) / azure / none ----------
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
if LLM_PROVIDER not in {"ollama", "azure", "none"}:
    print(f"⚠️  Unknown LLM_PROVIDER '{LLM_PROVIDER}', using 'none' (local rules)")
    LLM_PROVIDER = "none"
AI_TIMEOUT = _float("AI_TIMEOUT", 60)   # seconds; Ollama on a laptop CPU is slow

# ---------- Ollama ----------
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip().rstrip("/")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "llama3.2:3b").strip()
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text").strip()

# ---------- Azure OpenAI (used when LLM_PROVIDER=azure) ----------
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21").strip()
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()                 # chat model
AZURE_OPENAI_EMBEDDING_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "").strip()  # embeddings

# ---------- What is switched ON (filled in at startup by ai_service.check_ai()) ----------
# Before the startup check these say what is *configured*; the check then turns them off
# if Ollama is not running or a model is missing.
if LLM_PROVIDER == "azure":
    AI_ENABLED = bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT)
    EMBEDDINGS_ENABLED = AI_ENABLED and bool(AZURE_OPENAI_EMBEDDING_DEPLOYMENT)
elif LLM_PROVIDER == "ollama":
    AI_ENABLED = bool(OLLAMA_CHAT_MODEL)
    EMBEDDINGS_ENABLED = bool(OLLAMA_EMBED_MODEL)
else:
    AI_ENABLED = False
    EMBEDDINGS_ENABLED = False

AI_MODE_LABEL = "Local demo mode (no AI)"   # shown in /health and the top bar
