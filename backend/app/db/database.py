from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker, declarative_base
from app import config
from app.config import DATABASE_URL, DB_ECHO

# SQLite needs one extra setting; PostgreSQL does not
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {"connect_timeout": 10}

engine = create_engine(
    DATABASE_URL,
    echo=DB_ECHO,
    pool_pre_ping=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def safe_url() -> str:
    """The database URL with the password replaced by ***."""
    return engine.url.render_as_string(hide_password=True)


def check_connection(quiet: bool = False) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:  # noqa: BLE001
        if not quiet:
            print(f"❌ Database connection failed → {safe_url()}")
            print(f"   Reason: {str(e).splitlines()[0] if str(e) else e.__class__.__name__}")
            if engine.url.get_backend_name() == "postgresql":
                print("   Check: is PostgreSQL running? Does the database exist? Is the password in backend/.env correct?")
        return False


def old_structure_problems() -> list:
    """Tables that exist but are missing columns this code needs (left over from an older version)."""
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    problems = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        have = {c["name"] for c in insp.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in have]
        if missing:
            problems.append(f"{table.name} (missing: {', '.join(missing)})")
    return problems


def init_db() -> bool:
    """Connect, create tables, add demo data if the products table is empty, compute embeddings."""
    from app.models import tables  # noqa: F401  (registers all tables)
    from app.models.tables import Product
    from app.services.seed import seed_if_empty
    from app.services import product_search

    if not check_connection():
        return False
    print(f"✅ Database connected successfully → {safe_url()}")

    problems = old_structure_problems()
    if problems:
        print("⚠️  Some tables have an OLD structure: " + "; ".join(problems))
        print("   Fix: stop the backend, run  python reset_db.py  in the backend folder, then start again.")

    Base.metadata.create_all(bind=engine)
    print("✅ Tables ready")
    db = SessionLocal()
    try:
        if seed_if_empty(db):
            print("✅ Demo data added (products, customers, salespeople)")
        print(f"📦 Products in database: {db.query(Product).count()}")
        try:
            n = product_search.ensure_embeddings(db, verbose=True)
            if n:
                print(f"✅ Embeddings saved for {n} product(s)")
            elif config.EMBEDDINGS_ENABLED:
                print("✅ Embeddings already up to date")
        except Exception as e:  # noqa: BLE001
            db.rollback()
            print(f"⚠️  Could not compute embeddings now ({e}). Search will use the local fuzzy score.")
    except Exception as e:  # noqa: BLE001
        print(f"❌ Database setup failed: {str(e).splitlines()[0]}")
        if problems:
            print("   This is probably the old table structure. Run  python reset_db.py")
        return False
    finally:
        db.close()
    return True


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
