import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        # 日线库 4.4GB，默认 2000 页缓存 + 临时表落盘会让全市场聚合慢 3~4 倍
        # （箱体统计实测 135s → 28s），所以把页缓存提到 256MB、排序/临时表放内存。
        _conn.execute("PRAGMA cache_size=-262144")
        _conn.execute("PRAGMA temp_store=MEMORY")
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        with _lock:
            _conn.executescript(schema)
            _conn.commit()
    return _conn


@contextmanager
def transaction():
    """一批写入要么全部生效要么全部回滚（本地导入换表、批量落库用）。"""
    conn = get_conn()
    with _lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        cur = get_conn().execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, args: tuple = ()):
    rows = query(sql, args)
    return rows[0] if rows else None


def query_rows(sql: str, args: tuple = ()) -> list:
    """只要原始行、不建 dict：几十万行的聚合结果用（dict 版内存和耗时都要翻倍以上）。"""
    with _lock:
        cur = get_conn().execute(sql, args)
        return cur.fetchall()


def execute(sql: str, args: tuple = (), many: list | None = None):
    with _lock:
        conn = get_conn()
        if many is not None:
            conn.executemany(sql, many)
        else:
            conn.execute(sql, args)
        conn.commit()


def kv_get(key: str, default=None):
    row = query_one("SELECT value FROM kv_store WHERE key=?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def kv_set(key: str, value) -> None:
    with _lock:
        get_conn().execute(
            "INSERT INTO kv_store(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        get_conn().commit()
