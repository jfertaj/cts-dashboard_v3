# init_db.py
from app.database import Base, engine
from app import models

def reset_database():
    print("🧨 Dropping existing tables...")
    Base.metadata.drop_all(bind=engine)
    print("📦 Creating fresh tables...")
    Base.metadata.create_all(bind=engine)
    print("✅ Database reset complete.")

if __name__ == "__main__":
    reset_database()