from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/cts-dashboard-db")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,   # validates connection before use
    pool_recycle=1800,    # recycle connections every 30 min to avoid stale sockets
    pool_size=5,          # steady-state connections per worker (uvicorn uses 4 workers)
    max_overflow=10,      # allow up to 10 extra connections under burst load
    pool_timeout=10,      # raise after 10 s waiting for a free connection (avoids deadlocks)
    future=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()