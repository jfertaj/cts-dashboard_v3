# app/startup.py
import logging
from app.database import Base, engine

logger = logging.getLogger("cts-backend.startup")

def _load_models():
    """
    Importa módulos de modelos para que SQLAlchemy registre todas las tablas
    antes de create_all(). Sin esto, la metadata estará vacía y no creará nada.
    """
    # Import “de lado” (side-effect) para registrar subclasses de Base
    from app import models  # noqa: F401

def init_db():
    """Crea todas las tablas declaradas en modelos si no existen."""
    print("🧱 Inicializando base de datos...")
    _load_models()
    Base.metadata.create_all(bind=engine)
    print("✅ Tablas creadas (si no existían)")

def load_geonames(path: str):
    """Carga ciudades desde cities500.txt en la tabla geonames_cities si está vacía."""
    from app.models import GeonameCity  # import perezoso
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        if db.query(GeonameCity).count() > 0:
            print("⚠️ geonames ya está cargado, se omite.")
            return

        print(f"🗺️ Cargando ciudades desde {path}...")
        with open(path, encoding="utf-8") as f:
            for line in f:
                fields = line.strip().split("\t")
                if len(fields) < 19:
                    continue
                db.add(GeonameCity(
                    geonameid=int(fields[0]),
                    name=fields[1],
                    asciiname=fields[2],
                    alternatenames=fields[3],
                    latitude=float(fields[4]),
                    longitude=float(fields[5]),
                    feature_class=fields[6],
                    feature_code=fields[7],
                    country_code=fields[8],
                    cc2=fields[9],
                    admin1_code=fields[10],
                    admin2_code=fields[11],
                    admin3_code=fields[12],
                    admin4_code=fields[13],
                    population=int(fields[14]) if fields[14].isdigit() else 0,
                    elevation=fields[15],
                    dem=fields[16],
                    timezone=fields[17],
                    modification_date=fields[18],
                ))
        db.commit()
        print("🌍 Ciudades cargadas en la base de datos.")
    finally:
        db.close()

def sync_salesforce_subaccounts():
    logger.info("🔄 sync_salesforce_subaccounts(): placeholder ejecutado (sin acciones).")