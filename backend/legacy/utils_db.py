# app/utils_db.py
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import SessionLocal, engine


def get_db() -> Generator[Session, None, None]:
    """
    Dependencia FastAPI para inyectar una sesión DB por request.
    Cierra siempre aunque la ruta lance excepción.
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """
    Context manager útil fuera de rutas (jobs, startups, etc.).
    """
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_db() -> bool:
    """
    Comprueba la conectividad ejecutando SELECT 1.
    Devuelve True si OK, False si falla.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False