"""Run 日常刷新 against the live server and assert it never touches kline_daily.

Usage: python backend/scripts/check_refresh.py [base_url]
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8610"


def snap() -> dict:
    return {
        "kline_rows": db.query_one("SELECT COUNT(*) n FROM kline_daily")["n"],
        "kline_max": db.query_one("SELECT MAX(date) d FROM kline_daily")["d"],
        "kline_codes": db.query_one("SELECT COUNT(DISTINCT code) c FROM kline_daily")["c"],
        "kline_sum_close": round(db.query_one(
            "SELECT SUM(close) s FROM kline_daily WHERE date=(SELECT MAX(date) FROM kline_daily)")["s"], 2),
        "fund_max": db.query_one("SELECT MAX(trade_date) d FROM fundamentals")["d"],
        "fund_n": db.query_one("SELECT COUNT(*) n FROM fundamentals")["n"],
        "idx_max": db.query_one("SELECT MAX(date) d FROM index_kline")["d"],
        "idx_codes": db.query_one("SELECT COUNT(DISTINCT index_code) c FROM index_kline")["c"],
        "fin_rows": db.query_one("SELECT COUNT(*) n FROM fin_report")["n"],
        "stock_n": db.query_one("SELECT COUNT(*) n FROM stocks WHERE is_active=1")["n"],
    }


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read())


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


before = snap()
print("before:", json.dumps(before, ensure_ascii=False), flush=True)
post("/api/data/incremental", {})

deadline = time.time() + 2400
job = {}
while time.time() < deadline:
    time.sleep(20)
    st = get("/api/data/status")
    job = st["incremental"]
    print(f"  [{job.get('status')}] {job.get('progress')}/{job.get('total')} {job.get('message')}", flush=True)
    if "incremental" not in st["running"]:
        break

after = snap()
print("after: ", json.dumps(after, ensure_ascii=False), flush=True)
assert job.get("status") == "done", f"任务未正常结束: {job}"
for k in ("kline_rows", "kline_max", "kline_codes", "kline_sum_close"):
    assert after[k] == before[k], f"日线被在线任务改动了：{k} {before[k]} -> {after[k]}"
assert "kline_source" not in get("/api/data/status"), "data_status 仍在暴露 kline_source"
print("PASS 日线库一行未动；列表/快照/财报/指数刷新完成", flush=True)
