import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import init_db
from app.api.routes import router
from app.services import ai_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Starting TD Africa Order Bot (POC)")
    ai_service.check_ai()      # first: decides if Ollama/Azure is ON (embeddings need this)
    init_db()                  # connect, create tables, seed if empty, compute missing embeddings
    # load the chat model in the background so the first chat message is faster
    threading.Thread(target=ai_service.warm_up, daemon=True).start()
    print("📖 API docs: http://127.0.0.1:8000/docs")
    yield
    print("🛑 Server stopped")


app = FastAPI(title="TD Africa Order Bot (POC)", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
