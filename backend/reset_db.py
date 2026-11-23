"""
Drop ALL tables of this app, create them again with the current structure, and load fresh demo data
(plus product embeddings if Ollama is running).

Run it when the backend says "Some tables have an OLD structure", or whenever you want a clean database:

    cd backend
    venv\\Scripts\\activate
    python reset_db.py          (asks for confirmation)
    python reset_db.py --yes    (no question)

Stop the backend first, so it is not holding the tables open.
"""
import sys

from sqlalchemy import text

from app import config
from app.db.database import Base, engine, SessionLocal, check_connection, safe_url
from app.models import tables  # noqa: F401  (registers all tables)
from app.models.tables import Product
from app.services import ai_service, product_search
from app.services.seed import seed


def main():
    print(f"Database: {safe_url()}")
    if not check_connection():
        sys.exit(1)
    if "--yes" not in sys.argv:
        answer = input("This DELETES all orders, chats and products in this database. Type YES to continue: ")
        if answer.strip().upper() != "YES":
            print("Cancelled.")
            return

    names = [t.name for t in reversed(Base.metadata.sorted_tables)]
    with engine.begin() as conn:
        for name in names:
            if engine.url.get_backend_name() == "postgresql":
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}" CASCADE'))
            else:
                conn.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
    print(f"🗑️  Dropped tables: {', '.join(names)}")

    Base.metadata.create_all(bind=engine)
    print("✅ Tables created")

    ai_service.check_ai()
    db = SessionLocal()
    try:
        seed(db)
        print(f"✅ Demo data added. Products in database: {db.query(Product).count()}")
        try:
            n = product_search.ensure_embeddings(db, verbose=True)
            if n:
                print(f"✅ Embeddings saved for {n} product(s)")
        except Exception as e:  # noqa: BLE001
            db.rollback()
            print(f"⚠️  Embeddings not computed ({e}). They will be computed when the backend starts.")
        if not config.EMBEDDINGS_ENABLED:
            print("ℹ️  Embeddings are off right now; they will be computed when the backend starts with Ollama running.")
    finally:
        db.close()
    print("Done. Start the backend again.")


if __name__ == "__main__":
    main()
