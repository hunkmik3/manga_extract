from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from flowboard.config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_conn, _connection_record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    # Concurrency: the single agent process serves HTTP + runs the async worker
    # (several gens at once) against this one SQLite file. In the default
    # rollback-journal mode a writer takes a whole-DB exclusive lock, so under
    # concurrent gens + polls + status writes everything serializes and stalls.
    #   - WAL: readers and the writer run concurrently (no reader/writer block).
    #   - busy_timeout: wait up to 5s for a lock instead of erroring "database
    #     is locked" immediately.
    #   - synchronous=NORMAL: safe under WAL, far fewer fsyncs (faster writes).
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def init_db() -> None:
    from sqlalchemy import inspect

    from flowboard.db import models

    # Targeted migration: if an older `asset` table exists without `url`,
    # drop it. Acceptable because the app has not stored real asset rows
    # prior to Run 6; other tables (board, node, edge, chatmessage, request)
    # are left alone.
    with engine.connect() as conn:
        insp = inspect(conn)
        if insp.has_table("asset"):
            cols = {c["name"] for c in insp.get_columns("asset")}
            if "url" not in cols:
                models.Asset.__table__.drop(conn, checkfirst=True)
                conn.commit()

        # Edge.source_variant_idx — added when per-edge variant pinning
        # shipped. SQLite ALTER TABLE ADD COLUMN is non-destructive (and
        # idempotent via the column-existence check), so existing DBs
        # pick up the new column on first boot without losing data.
        # `create_all` below won't help because it skips ALTERs on
        # existing tables.
        if insp.has_table("edge"):
            edge_cols = {c["name"] for c in insp.get_columns("edge")}
            if "source_variant_idx" not in edge_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE edge ADD COLUMN source_variant_idx INTEGER"
                )
                conn.commit()

        # Board.kind — separates Manga boards from Flow Studio projects. Existing
        # rows default to "manga" (they predate Flow Studio). Idempotent ALTER.
        if insp.has_table("board"):
            board_cols = {c["name"] for c in insp.get_columns("board")}
            if "kind" not in board_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE board ADD COLUMN kind VARCHAR DEFAULT 'manga'"
                )
                conn.commit()

        # ColorizeChapter.sheets — character-sheet references (outfit_id → media
        # id) added with the character-sheet pipeline. Idempotent ALTER so
        # existing chapters keep their data and pick up the column on boot.
        if insp.has_table("colorizechapter"):
            cz_cols = {c["name"] for c in insp.get_columns("colorizechapter")}
            if "sheets" not in cz_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE colorizechapter ADD COLUMN sheets JSON DEFAULT '{}'"
                )
                conn.commit()
            if "variants" not in cz_cols:
                conn.exec_driver_sql(
                    "ALTER TABLE colorizechapter ADD COLUMN variants JSON DEFAULT '{}'"
                )
                conn.commit()

    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session():
    with Session(engine) as session:
        yield session
