import os, sys
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context

# añade /backend al sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))         # backend/alembic
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..'))  # backend
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.database import Base
try:
    import app.models  # importa tus modelos para autogenerate
except Exception as e:
    print(f"⚠️ Aviso: {e}")

target_metadata = Base.metadata
DATABASE_URL = os.getenv("DATABASE_URL") or "postgresql+psycopg://user:pass@localhost:5432/cts_dashboard_db?sslmode=require"

def run_migrations_offline():
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_schemas=True,
        include_name=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = DATABASE_URL
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_schemas=True,
            include_name=True,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()