"""Seed a small sample dataset for development/demo (about 15 well-known stocks).

Usage: python backend/scripts/seed_sample.py
"""
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db
from app.datasource import tencent as tc, eastmoney as em
from app.datasource import updater

SAMPLE = {
    "600519": "贵州茅台", "000001": "平安银行", "600036": "招商银行",
    "300750": "宁德时代", "002594": "比亚迪", "688981": "中芯国际",
    "600536": "中国软件", "002230": "科大讯飞", "300308": "中际旭创",
    "600276": "恒瑞医药", "600030": "中信证券", "000858": "五粮液",
    "601899": "紫金矿业", "002415": "海康威视", "830799": "艾融软件",
}

today = dt.date.today().isoformat()

# 1) resolve industries/valuation from Tencent boards
print("fetching boards…")
boards = tc.fetch_industry_boards()
print("boards:", len(boards))
found = {}
for b in boards:
    if len(found) >= len(SAMPLE):
        break
    try:
        items = tc.fetch_board_stocks(b["board_code"])
    except Exception as e:
        print("  board fail", b["board_code"], str(e)[:60]); continue
    for it in items:
        if it["code"] in SAMPLE and it["code"] not in found:
            it["board_code"] = b["board_code"]
            it["board_name"] = b["board_name"]
            found[it["code"]] = it
print("resolved stocks:", len(found))

srows, frows = [], []
for code, it in found.items():
    market = em.market_for(code)
    srows.append({"code": code, "name": it["name"], "market": market,
                  "secid": em.secid_for(code), "industry": it["board_name"],
                  "board_code": it["board_code"],
                  "list_date": None, "is_active": 1})
    frows.append({"code": code, "trade_date": today, "price": it["price"],
                  "pct_chg": it["pct_chg"], "total_mv": it["total_mv"],
                  "float_mv": it["float_mv"], "pe_dynamic": None,
                  "pe_ttm": it["pe_ttm"], "pb": it["pb"],
                  "turnover_rate": None, "amount": None})

db.execute("INSERT INTO boards(board_code,board_name,board_type) VALUES(?,?,?) "
           "ON CONFLICT(board_code) DO UPDATE SET board_name=excluded.board_name",
           many=[(b["board_code"], b["board_name"], "industry") for b in boards])
db.execute("INSERT INTO stocks(code,name,market,secid,industry,board_code,list_date,is_active) "
           "VALUES(:code,:name,:market,:secid,:industry,:board_code,:list_date,:is_active) "
           "ON CONFLICT(code) DO UPDATE SET name=excluded.name,market=excluded.market,"
           "secid=excluded.secid,industry=excluded.industry,board_code=excluded.board_code", many=srows)
db.execute("INSERT INTO fundamentals(code,trade_date,price,pct_chg,total_mv,float_mv,pe_dynamic,pe_ttm,pb,turnover_rate,amount) "
           "VALUES(:code,:trade_date,:price,:pct_chg,:total_mv,:float_mv,:pe_dynamic,:pe_ttm,:pb,:turnover_rate,:amount) "
           "ON CONFLICT(code) DO UPDATE SET trade_date=excluded.trade_date,price=excluded.price,"
           "pct_chg=excluded.pct_chg,total_mv=excluded.total_mv,float_mv=excluded.float_mv,"
           "pe_ttm=excluded.pe_ttm,pb=excluded.pb", many=frows)

# 2) klines for each sample stock (full history, Tencent)
total_rows = 0
for code in found:
    tcode = f"{em.market_for(code).lower()}{code}"
    try:
        rows = tc.fetch_kline(tcode)
    except Exception as e:
        print("kline fail", code, str(e)[:80]); continue
    updater._upsert_klines(code, rows)
    total_rows += len(rows)
    print(f"  {code}: {len(rows)} bars {rows[0]['date'] if rows else '-'} .. {rows[-1]['date'] if rows else '-'}")

# 3) indexes
updater._refresh_indexes("tc")

# 4) financial reports (EM datacenter)
for q in ["2026-06-30", "2025-12-31", "2025-09-30"]:
    try:
        items = em.fetch_financial_report(q)
    except Exception as e:
        print("fin fail", q, str(e)[:60]); continue
    if items:
        db.execute("INSERT INTO fin_report(code,report_date,eps,revenue,revenue_yoy,net_profit,net_profit_yoy,roe_weighted,gross_margin) "
                   "VALUES(:code,:report_date,:eps,:revenue,:revenue_yoy,:net_profit,:net_profit_yoy,:roe_weighted,:gross_margin) "
                   "ON CONFLICT(code,report_date) DO UPDATE SET eps=excluded.eps,revenue=excluded.revenue,"
                   "revenue_yoy=excluded.revenue_yoy,net_profit=excluded.net_profit,"
                   "net_profit_yoy=excluded.net_profit_yoy,roe_weighted=excluded.roe_weighted,gross_margin=excluded.gross_margin",
                   many=items)
    print(f"fin {q}: {len(items)}")

# 5) default pools
if not db.query("SELECT id FROM pools"):
    for name in ("核心持仓", "观察池", "打板池"):
        db.execute("INSERT INTO pools(name) VALUES(?)", (name,))

st = updater.get_status()
st["stock_count"] = len(found)
st["provider"] = "tc"
st["sample_seeded"] = True
db.kv_set("data_status", st)
print("DONE stocks:", len(found), "kline rows:", total_rows)
