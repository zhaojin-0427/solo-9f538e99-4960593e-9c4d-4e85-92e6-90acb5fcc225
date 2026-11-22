from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    version       INTEGER NOT NULL DEFAULT 0,
    base_version  INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS row_def (
    row_key TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    pos     INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS col_def (
    col_key TEXT PRIMARY KEY,
    label   TEXT NOT NULL,
    pos     INTEGER NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS base_cell (
    row_key     TEXT NOT NULL REFERENCES row_def(row_key) ON DELETE CASCADE,
    col_key     TEXT NOT NULL REFERENCES col_def(col_key) ON DELETE CASCADE,
    amount_yuan INTEGER NOT NULL CHECK (amount_yuan >= 0),
    PRIMARY KEY (row_key, col_key)
);

CREATE TABLE IF NOT EXISTS public_cell (
    row_key   TEXT NOT NULL REFERENCES row_def(row_key) ON DELETE CASCADE,
    col_key   TEXT NOT NULL REFERENCES col_def(col_key) ON DELETE CASCADE,
    value_wan INTEGER NOT NULL CHECK (value_wan >= 0),
    PRIMARY KEY (row_key, col_key)
);

CREATE TABLE IF NOT EXISTS public_row (
    row_key      TEXT PRIMARY KEY REFERENCES row_def(row_key) ON DELETE CASCADE,
    subtotal_wan INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS public_col (
    col_key      TEXT PRIMARY KEY REFERENCES col_def(col_key) ON DELETE CASCADE,
    subtotal_wan INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS public_total (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    total_wan INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS lock_record (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    row_key           TEXT NOT NULL UNIQUE REFERENCES row_def(row_key) ON DELETE CASCADE,
    row_label         TEXT NOT NULL,
    subtotal_wan      INTEGER NOT NULL,
    cells_json        TEXT NOT NULL,
    statement_version INTEGER NOT NULL,
    locked_at         TEXT NOT NULL
);

-- 锁定留档：与锁定记录同时写入；换表（重导 CSV）清空锁定但保留留档。
-- 故意不对 row_def/lock_record 设外键，旧档在换表后仍可按原名称与版号查询。
CREATE TABLE IF NOT EXISTS archive_record (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    lock_id           INTEGER NOT NULL UNIQUE,
    row_label         TEXT NOT NULL,
    statement_version INTEGER NOT NULL,
    col_labels_json   TEXT NOT NULL,
    cells_json        TEXT NOT NULL,
    subtotal_wan      INTEGER NOT NULL,
    base_json         TEXT,
    base_status       TEXT NOT NULL CHECK (base_status IN ('recorded', 'unavailable')),
    locked_at         TEXT NOT NULL,
    archived_at       TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta (id, version, base_version, updated_at) VALUES (1, 0, 0, ?)",
            (now_iso(),),
        )
        backfill_archives(conn)
    finally:
        conn.close()


def backfill_archives(conn: sqlite3.Connection) -> None:
    """为仍在的锁定补建留档（旧库升级迁移）。幂等：已留档的锁定不再改动。

    只有版号与锁定记录能确认该行锁定后底表从未更正，才用现有底表补记元金额；
    证据不足时保留原公示数字，元金额标为「未留存」（base_status='unavailable'）。
    判断依据：每次变更（换表/更正/锁定）都使版号 +1；换表会清空锁定，故现存锁定
    之后只可能有锁定与更正两类事件，减去可数的锁定事件即得更正次数。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        pending = conn.execute(
            "SELECT l.id, l.row_key, l.row_label, l.subtotal_wan, l.cells_json, "
            "       l.statement_version, l.locked_at "
            "FROM lock_record l "
            "WHERE NOT EXISTS (SELECT 1 FROM archive_record a WHERE a.lock_id = l.id) "
            "ORDER BY l.id").fetchall()
        if pending:
            version = conn.execute(
                "SELECT version FROM meta WHERE id = 1").fetchone()["version"]
            lock_versions = [r["statement_version"] for r in conn.execute(
                "SELECT statement_version FROM lock_record")]
            col_defs = conn.execute(
                "SELECT col_key, label FROM col_def ORDER BY pos").fetchall()
            col_keys = [c["col_key"] for c in col_defs]
            col_labels = [c["label"] for c in col_defs]
            ts = now_iso()
            for lock in pending:
                locks_after = sum(1 for v in lock_versions
                                  if v > lock["statement_version"])
                corrections_after = (version - lock["statement_version"] - 1
                                     - locks_after)
                base_amounts = None
                if corrections_after == 0 and col_keys:
                    # 锁定后底表从未更正：现有底表即锁定时的底表，可补记元金额
                    amounts = {r["col_key"]: r["amount_yuan"] for r in conn.execute(
                        "SELECT col_key, amount_yuan FROM base_cell WHERE row_key = ?",
                        (lock["row_key"],))}
                    if all(k in amounts for k in col_keys):
                        base_amounts = [amounts[k] for k in col_keys]
                conn.execute(
                    "INSERT INTO archive_record (lock_id, row_label, statement_version, "
                    "col_labels_json, cells_json, subtotal_wan, base_json, base_status, "
                    "locked_at, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (lock["id"], lock["row_label"], lock["statement_version"],
                     json.dumps(col_labels, ensure_ascii=False), lock["cells_json"],
                     lock["subtotal_wan"],
                     json.dumps(base_amounts, ensure_ascii=False)
                     if base_amounts is not None else None,
                     "recorded" if base_amounts is not None else "unavailable",
                     lock["locked_at"], ts))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


@contextmanager
def write_transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE: 串行化写事务，版本检查与写入之间不会被其他写者插入。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
