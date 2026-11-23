# POC Requirements · TD Africa WhatsApp AI Order Bot

## 1. Goal of the POC
Prove that an AI assistant can **take orders from chat messages** safely:
- It understands messy messages (Nigerian English, Pidgin, typos, nicknames).
- It finds the right product, and **price, stock and credit always come from the database**.
- It hands the chat to the **salesperson** whenever a human is needed.

## 2. In scope (what the POC does)

| # | Requirement | How the POC meets it |
|---|---|---|
| R1 | Identify the customer from the phone number | Customer table lookup, giving shop, salesperson and credit |
| R2 | Understand the message | Azure OpenAI, or a rule-based parser when no key is set |
| R3 | Find products from informal words (RAG) | `product_search.py`: fuzzy/semantic retrieval plus a nickname list |
| R4 | Ask only what is missing, one question at a time | "Which printer? 1 or 2", "How many?" |
| R5 | Validate against real data | Stock, price and credit from the database. The AI never guesses. |
| R6 | Show a clear summary and require YES | Order summary card with YES / CHANGE |
| R7 | Create the order only after YES | Order ID returned, stock and credit updated |
| R8 | Three paths | **A** auto, **B** assisted (questions asked), **C** salesperson |
| R9 | Hand-off triggers | Discount, big order, low credit, blocked account, unknown product, repeated confusion, "talk to a person" |
| R10 | AI stays quiet while the salesperson is in the chat | Conversation `mode = human` |
| R11 | Salesperson can reply, take over and hand back | Salesperson dashboard |
| R12 | Salesperson is told about orders the AI placed | "Orders placed by AI" list |
| R13 | Reorder and order status | "Same as last time", "Where is my order?" |
| R14 | New (unknown) number becomes a lead | Asks for business details, then assigns a salesperson |
| R15 | Every message is logged and attributed | `messages` table: customer / ai / salesperson / system |
| R16 | Business limits changeable without code | `.env`: BIG_ORDER_AMOUNT, BIG_ORDER_QTY, MAX_CLARIFY_TRIES |

## 3. Out of scope (later phases)
- Real WhatsApp number (Meta Cloud API / BSP), message templates, and the 24-hour window
- Real SuperApp APIs (the POC uses its own database with sample data)
- Moving salespeople's numbers, logins and roles on the dashboard
- Yoruba, Igbo and Hausa beyond simple phrases; voice notes and images
- Delivery and dispatch notifications, and the metrics dashboard
- Self-hosted LLM (Option B)

## 4. Tech stack
| Part | Choice |
|---|---|
| Frontend | React 18 + Vite |
| Backend | Python 3.10+ · FastAPI · SQLAlchemy |
| Database | SQLite (default) or PostgreSQL |
| AI (optional) | Azure OpenAI: chat model + embeddings model |
| RAG | Retrieval: local TF-IDF/fuzzy search, plus Azure embeddings when set. Reasoning: the AI picks from the retrieved products. |

## 5. What is needed to run it
- A Windows laptop with **Python 3.10+** and **Node.js 18+**
- Internet only for the first install (`1_setup.bat`)
- Optional: an Azure OpenAI endpoint, key and deployment names

## 6. Demo success criteria
1. A normal order is completed by the AI alone (Path A) with the correct total.
2. An unclear product leads to a question, then the order (Path B).
3. A discount request stops the AI, the salesperson replies, then hands back with an approved discount (Path C).
4. Low credit and a blocked account both go to the salesperson. No order is created.
5. No order is ever created without the customer's YES.
6. Products added on the Products page can be found by the chat right away.

## 7. Needed from TD Africa for the next phase
- SuperApp sandbox APIs: partner lookup, product, price, stock, credit, order create/status
- The real product list with the nicknames partners use
- The partner-to-salesperson list, with backup rules
- Sample (anonymised) WhatsApp conversations for testing
- A WhatsApp Business Account plus 1–2 pilot salesperson numbers
