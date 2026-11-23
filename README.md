# TD Africa · WhatsApp AI Order Bot · POC

A working demo of the AI order assistant from the Trysol proposal.
A **web chat** stands in for WhatsApp. The order logic is the same and can be connected to WhatsApp later.

- **Frontend:** React + Vite, in `frontend/`
- **Backend:** Python + FastAPI, in `backend/`
- **Product search:** RAG retrieval. The search finds the product, then price and stock come from the database.
- **Database:** PostgreSQL (database `td_africa`). SQLite still works as a fallback.
- **AI:** **Ollama** (free, runs on the laptop): `llama3.2:3b` for understanding, `nomic-embed-text` for embeddings.
  Azure OpenAI still works (`LLM_PROVIDER=azure`). If Ollama is not running, the bot uses the local rules (**Local demo mode**).
- **RAG:** each product's embedding is stored in the `product_embeddings` table (JSON float array, no pgvector).

---

## 1. What you need on the laptop (once)

| Tool | Version | Check with |
|---|---|---|
| Python | 3.10 or newer | `python --version` |
| Node.js | 18 or newer (LTS) | `node --version` |
| PostgreSQL | 13 or newer (with pgAdmin) | `psql --version` |
| Ollama | latest, from https://ollama.com/download | `ollama --version` |

### PostgreSQL (once)
1. In pgAdmin: right-click **Databases → Create → Database…**, name it `td_africa`, Save.
   (Or in a terminal: `psql -U postgres -c "CREATE DATABASE td_africa;"`)
2. Open `backend/.env` and replace `YOUR_PASSWORD` in this line with your `postgres` password:
   ```
   DATABASE_URL=postgresql+psycopg2://postgres:YOUR_PASSWORD@localhost:5432/td_africa
   ```
   If the password has special characters, write them URL-encoded:
   `@` → `%40`, `#` → `%23`, `%` → `%25`, `:` → `%3A`, `/` → `%2F` (example: `Pa@ss#1` → `Pa%40ss%231`).
3. Nothing else: the backend creates the tables and loads the demo products on first start.

### Ollama (once)
1. Install Ollama from https://ollama.com/download and open it (it runs in the system tray).
2. Download the two models (about 2.3 GB in total):
   ```powershell
   ollama pull llama3.2:3b
   ollama pull nomic-embed-text
   ```
3. Check: `ollama list` shows both models, and http://localhost:11434 says "Ollama is running".

## 2. Install (once)

Double-click **`1_setup.bat`**.
It creates the Python virtual environment, installs `backend/requirements.txt`, and runs `npm install`.
If the internet drops, just run it again.

## 3. Start the demo

Double-click **`2_start_demo.bat`**.
- Two black windows open (backend and frontend). Keep them open.
- The browser opens **http://localhost:5173**
- API docs: **http://127.0.0.1:8000/docs**

Start the **Ollama** app first (tray icon), then the demo.

The backend window should show:
```
🤖 AI mode: Ollama (llama3.2:3b)
🔎 Embeddings: Ollama (nomic-embed-text)
✅ Database connected successfully → postgresql+psycopg2://postgres:***@localhost:5432/td_africa
✅ Tables ready
✅ Demo data added (products, customers, salespeople)      ← first start only
📦 Products in database: 10
✅ Embeddings saved for 10 product(s)                       ← or "already up to date"
🔥 Ollama chat model loaded and ready
```
The top bar in the browser shows **Backend OK · Ollama (llama3.2:3b)**.
Ollama on a laptop CPU is slow: each chat reply takes about **5–15 seconds** (the very first one up to ~40 s).

**Manual start** (if you prefer the VS Code terminal):
```powershell
# terminal 1
cd backend
venv\Scripts\activate
python -m uvicorn app.main:app --reload --reload-dir app --port 8000

# terminal 2
cd frontend
npm run dev
```

---

## 4. Demo script (about 10 minutes)

Click **Reset demo** (top right) before you start.

| # | Tab | Do this | What it shows |
|---|---|---|---|
| 1 | Customer chat | Customer **Tunde** → "Good morning, I want 10 HP laptops" → **YES** | **Path A:** the AI finds the product, checks price, stock and credit, and places the order. |
| 2 | Customer chat | "Where is my order?" | Order status comes from live data. |
| 3 | Customer chat | "printer 4" → pick **2** | **Path B:** the AI asks which product, then completes the order. |
| 4 | Customer chat | "hp 15 x5 and 2 chargers" | Two products in one message; "hp 15" is a nickname. |
| 5 | Customer chat | "Same as last time" | Reorder with today's prices. |
| 6 | Customer chat | "I want 1000 HP laptops. Give me 10% discount" | The AI **stops** and passes the chat to salesperson Chidi. |
| 7 | Salesperson dashboard | Open Tunde → reply "I can do 5% for 1000 units. OK?" → Discount **5** → **Hand back to AI** | **Path C:** the human decides, then the AI sends the summary with the discount. |
| 8 | Customer chat | Click **YES** | Order placed with the discount. |
| 9 | Customer chat | Customer **Bola** → "5 dell laptops" | Not enough credit, so it goes to the salesperson. |
| 10 | Customer chat | Customer **Emeka** → any order | Blocked account, so it goes to the salesperson. |
| 11 | Customer chat | New number `09000000000` → "hi" → "Kemi Stores, Lekki, I need printers" | A new customer becomes a lead, assigned to a salesperson. |
| 12 | Products & orders | Search "lapptop", "hp 15", "ink printer" | Shows the RAG retrieval scores. Also add your own product. |

---

## 5. Settings: `backend/.env`

| Setting | Meaning |
|---|---|
| `DATABASE_URL` | PostgreSQL connection (see section 1). Put `#` in front and use the `sqlite:///` line to go back to SQLite. |
| `LLM_PROVIDER` | `ollama` (default), `azure`, or `none` (local rules only) |
| `OLLAMA_BASE_URL`, `OLLAMA_CHAT_MODEL`, `OLLAMA_EMBED_MODEL` | Where Ollama runs and which models to use |
| `AI_TIMEOUT` | Seconds to wait for one AI answer (default 60) |
| `BIG_ORDER_AMOUNT`, `BIG_ORDER_QTY` | Orders at or above these go to the salesperson. |
| `MAX_CLARIFY_TRIES` | After this many "didn't understand", go to the salesperson. |
| `AZURE_OPENAI_*` | Fill these to turn on real AI (see below). |

Restart the backend after changing `.env`.

### Old tables? Reset the database
If the backend prints **"Some tables have an OLD structure"** (for example, tables made by an older version of this POC),
stop the backend and run:
```powershell
cd backend
venv\Scriptsctivate
python reset_db.py
```
Type `YES`. It drops this app's tables, creates them again, loads the demo data and computes the embeddings.
(The **Reset demo** button does the same while the backend is running.)

### Use Azure OpenAI instead of Ollama
Set `LLM_PROVIDER=azure`, then fill `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_DEPLOYMENT` (your chat model deployment name).
Optionally, fill `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (for example `text-embedding-3-small`) for semantic search.
After a restart, the top bar shows **Azure OpenAI**.
If an AI call fails, the bot automatically falls back to the local rules, so the demo never breaks.

---

## 6. How it works (backend)

```
Customer message
   → order_flow.py   (the 7 steps: identity, intent, clarify, validate, confirm, execute, feedback)
      → ai_service.py        understand the message (Ollama/Azure; safety-checked against the rules in message_parser.py)
      → product_search.py    RAG retrieval: 50% local fuzzy score + 50% cosine similarity of stored embeddings
      → ai_service.py        RAG reasoning: if retrieval is unclear, the AI picks from the retrieved list
      → database             real price, stock, credit
      → rules                discount / big order / low credit / "talk to a person" → salesperson
      → order created only after the customer says YES
```

```
td-africa-poc/
├── 1_setup.bat, 2_start_demo.bat, run_backend.bat, run_frontend.bat
├── POC_REQUIREMENTS.md
├── backend/
│   ├── .env                      settings (database password, AI provider)
│   ├── requirements.txt
│   ├── reset_db.py               drop + recreate all tables, reseed demo data
│   └── app/
│       ├── main.py               starts FastAPI
│       ├── config.py             reads .env
│       ├── api/routes.py         all URLs
│       ├── db/database.py        DB connection + auto-create tables
│       ├── models/tables.py      9 tables (incl. product_embeddings)
│       └── services/
│           ├── order_flow.py     the order workflow (main logic)
│           ├── product_search.py RAG retrieval
│           ├── ai_service.py     Ollama / Azure OpenAI (chat + embeddings)
│           ├── message_parser.py rule-based understanding (fallback)
│           └── seed.py           demo data
└── frontend/
    └── src/
        ├── App.jsx               tabs
        ├── api.js                calls the backend
        ├── components/ChatMessages.jsx
        └── pages/ChatPage.jsx, DashboardPage.jsx, ProductsPage.jsx
```

### Main API (full list at /docs)
| Method | URL | Use |
|---|---|---|
| POST | `/chat` | Customer sends a message |
| GET | `/conversations/{phone}/messages` | Chat history plus draft order |
| POST | `/conversations/{phone}/reply` | Salesperson replies |
| POST | `/conversations/{phone}/takeover` | Salesperson takes over |
| POST | `/conversations/{phone}/handback` | Give the chat back to the AI |
| GET | `/alerts` | Help-needed list and order notices |
| GET/POST | `/products` | List or add products |
| GET | `/search?q=hp 15` | See the RAG retrieval results |
| POST | `/demo/reset` | Fresh demo data |

---

## 7. Problems?

| Problem | Fix |
|---|---|
| Top bar says "Backend not reachable" | The backend window is closed or shows an error. Run `run_backend.bat`. |
| `pip` timeout errors | Slow internet. Run `1_setup.bat` again. |
| `'npm' is not recognized` | Install Node.js LTS, then restart the laptop. |
| Port 8000 or 5173 already in use | Close old backend/frontend windows, or restart the laptop. |
| Want a clean start | Click **Reset demo**, or run `python reset_db.py` in `backend`. |
| `password authentication failed` | Wrong password in `backend/.env` (remember to URL-encode `@ # % :`). |
| `database "td_africa" does not exist` | Create it in pgAdmin (section 1). |
| "Some tables have an OLD structure" | Stop the backend and run `python reset_db.py`. |
| Top bar says "Local demo mode (Ollama not running)" | Open the Ollama app, then restart the backend. |
| "model ... is not pulled" | Run the `ollama pull ...` command it prints. |
| Chat replies are slow | Normal on a CPU (5–15 s). Close other heavy apps, or use a smaller model. |
