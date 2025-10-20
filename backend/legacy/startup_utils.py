from sqlalchemy import inspect
from sqlalchemy.orm import Session
from app.models import Base
from app.load_cities import load_cities500
from sqlalchemy import text


def check_table_exists(session: Session, table_name: str) -> bool:
    inspector = inspect(session.bind)
    return inspector.has_table(table_name)

def initialize_database(session: Session):
    # Load cities only if the table is empty
    inspector = inspect(session.bind)
    if inspector.has_table("geonames_cities"):
        count = session.execute(text("SELECT COUNT(*) FROM geonames_cities")).scalar()
        if count == 0:
            print("[Startup] Loading cities500.txt...")
            load_cities500("/app/cities500.txt")
        else:
            print("[Startup] cities500.txt already loaded.")
    else:
        print("[Startup] geonames_cities table does not exist.")