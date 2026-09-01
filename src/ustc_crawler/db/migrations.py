from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine

from alembic import command

ALEMBIC_HEAD = "0004_sync_snapshots_and_indexes"


def _config(database_path: Path) -> Config:
    repository_root = Path(__file__).resolve().parents[3]
    config_path = repository_root / "alembic.ini"
    if not config_path.is_file():
        raise RuntimeError(f"Alembic configuration is missing: {config_path}")
    config = Config(str(config_path))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    return config


def upgrade_database(database_path: str | Path) -> None:
    """Upgrade a local database and fail if it cannot reach the known head."""

    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_config(path), "head")
    # WAL is a database-level setting.  Initialize it once after an explicit
    # migration instead of changing journal mode on every pooled connection.
    engine = create_engine(
        f"sqlite:///{path}",
        pool_size=1,
        max_overflow=0,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    finally:
        engine.dispose()
