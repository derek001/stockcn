"""Run 日常刷新 against the live server and assert it never touches kline_daily
but does fill every stock's list_date.

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
        "kline_sum_factor": round(db.query_one(
            "SELECT SUM(adj_factor) s FROM kline_daily WHERE date=(SELECT MAX(date) FROM kline_daily)")["s"], 2),
        "fund_max": db.query_one("SELECT MAX(trade_date) d FROM fundamentals")["d"],
        "fund_n": db.query_one("SELECT COUNT(*) n FROM fundamentals")["n"],
        # 在册但没拿到「最新交易日」快照的只数：真停牌允许少量，成批滞后就是补录判据写窄了
        "fund_stale": db.query_one(
            "SELECT COUNT(*) n FROM stocks s LEFT JOIN fundamentals f ON f.code=s.code "
            "WHERE s.is_active=1 AND (f.trade_date IS NULL OR f.trade_date < "
            "  (SELECT MAX(date) FROM kline_daily))")["n"],
        "idx_max": db.query_one("SELECT MAX(date) d FROM index_kline")["d"],
        "idx_codes": db.query_one("SELECT COUNT(DISTINCT index_code) c FROM index_kline")["c"],
        "fin_rows": db.query_one("SELECT COUNT(*) n FROM fin_report")["n"],
        "stock_n": db.query_one("SELECT COUNT(*) n FROM stocks WHERE is_active=1")["n"],
        "list_date_n": db.query_one(
            "SELECT COUNT(*) n FROM stocks WHERE list_date IS NOT NULL")["n"],
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
for k in ("kline_rows", "kline_max", "kline_codes", "kline_sum_close", "kline_sum_factor"):
    assert after[k] == before[k], f"日线被在线任务改动了：{k} {before[k]} -> {after[k]}"
assert "kline_source" not in get("/api/data/status"), "data_status 仍在暴露 kline_source"
assert after["list_date_n"] >= before["list_date_n"], \
    f"上市日期被在线任务抹掉了：{before['list_date_n']} -> {after['list_date_n']}"
assert after["list_date_n"] >= after["stock_n"] - 20, \
    f"上市日期覆盖不足：{after['list_date_n']}/{after['stock_n']}（只应缺最新几只还没披露上市日的新股）"
# 科创板/北交所自 2026-10-10 起也会走补录报价这一步：判据一旦又写窄成「只补不在 stocks 的」，
# 688/920 那近千只就会整批冻在旧交易日，这里直接失败给出来。
assert after["fund_stale"] <= 50, (
    f"刷新后仍有 {after['fund_stale']} 只在册股票没拿到最新交易日（{after['fund_max']}）的快照，"
    f"刷新前 {before['fund_stale']} 只——成批滞后说明补录判据不对")
print(f"PASS 日线库一行未动；列表/快照/财报/指数刷新完成；上市日期覆盖 "
      f"{after['list_date_n']}/{after['stock_n']}；"
      f"没拿到最新快照的 {before['fund_stale']} -> {after['fund_stale']} 只", flush=True)
