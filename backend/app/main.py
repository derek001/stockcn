"""FastAPI application: REST API + static frontend hosting."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import backtest as bt
from . import db, screening
from .config import FRONTEND_DIST
from .datasource import updater
from .indicators import add_boll, add_kdj, add_ma, add_macd, add_rsi, compute_all
from .strategies import discover

app = FastAPI(title="A股分析终端")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


# ---------------- data ----------------

@app.get("/api/data/status")
def data_status():
    return updater.get_status()


@app.post("/api/data/full")
def data_full():
    if updater.is_busy():
        raise HTTPException(409, "已有更新任务在运行")
    updater.start_job("full")
    return {"ok": True}


@app.post("/api/data/incremental")
def data_incremental():
    if updater.is_busy():
        raise HTTPException(409, "已有更新任务在运行")
    updater.start_job("incremental")
    return {"ok": True}


class QmtImportReq(BaseModel):
    raw_path: str               # 「不复权」导出目录（含 SH/SZ/BJ 的那一层）
    geo_path: str               # 「等比后复权」导出目录
    mode: str = "full"          # full | incremental
    force: bool = False         # 跳过双目录体检（口径风险由操作者确认）


@app.post("/api/data/qmt/preview")
def qmt_preview(body: QmtImportReq):
    try:
        return updater.preview_local(body.raw_path, body.geo_path)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/data/qmt/import")
def qmt_import(body: QmtImportReq):
    if updater.is_busy():
        raise HTTPException(409, "已有更新任务在运行")
    for label, p in (("不复权", body.raw_path), ("等比后复权", body.geo_path)):
        # Path("") 会变成当前目录，必须先判空，否则空路径会被当成合法目录
        if not p.strip() or not Path(p.strip()).is_dir():
            raise HTTPException(400, f"{label}目录不存在：{p}")
    if body.mode == "full":
        kind = "qmt_full"
    elif body.mode == "incremental":
        row = db.query_one("SELECT COUNT(*) AS n FROM kline_daily")
        if not row or not row["n"]:
            raise HTTPException(409, "日线库还是空的，请先执行一次「全量导入」建库，之后再做增量导入")
        kind = "qmt_incremental"
    else:
        raise HTTPException(400, "mode 需为 full 或 incremental")
    updater.start_job(kind, (body.raw_path, body.geo_path), body.force)
    return {"ok": True}


# ---------------- stocks ----------------

@app.get("/api/stocks")
def stocks(q: str = "", limit: int = 50):
    like = f"%{q}%"
    rows = db.query(
        "SELECT s.code,s.name,s.market,s.industry,f.price,f.pct_chg,f.pe_ttm,f.pb,f.total_mv "
        "FROM stocks s LEFT JOIN fundamentals f ON f.code=s.code "
        "WHERE s.is_active=1 AND (s.code LIKE ? OR s.name LIKE ?) "
        "ORDER BY f.total_mv DESC LIMIT ?", (like, like, limit))
    return rows


@app.get("/api/stocks/{code}/kline")
def stock_kline(code: str, start: str = "", end: str = "", adj: str = "raw",
                indicators: str = Query("ma,macd,boll", description="comma list")):
    """adj=raw 给交易所口径（与行情软件一致，默认），adj=hfq 给后复权（价格 × 因子）。

    指标跟随所选口径，跟行情软件的行为一致。
    """
    if adj not in ("raw", "hfq"):
        raise HTTPException(400, "adj 只能是 raw 或 hfq")
    px = ("open, high, low, close" if adj == "raw" else
          "open*adj_factor AS open, high*adj_factor AS high, "
          "low*adj_factor AS low, close*adj_factor AS close")
    rows = db.query(
        f"SELECT date,{px},volume,amount,pct_chg,adj_factor FROM kline_daily "
        "WHERE code=? AND (?=='' OR date>=?) AND (?=='' OR date<=?) ORDER BY date",
        (code, start, start, end, end))
    if not rows:
        raise HTTPException(404, "无K线数据，请先在「数据中心 → 本地数据（QMT 导入）」把 QMT 导出的日线导入")
    df = pd.DataFrame(rows)
    want = set(indicators.split(","))
    if "ma" in want:
        df = add_ma(df)
    if "macd" in want:
        df = add_macd(df)
    if "boll" in want:
        df = add_boll(df)
    if "rsi" in want:
        df = add_rsi(df)
    if "kdj" in want:
        df = add_kdj(df)
    info = db.query_one(
        "SELECT s.code,s.name,s.market,s.industry,s.list_date,"
        "f.price,f.pct_chg,f.total_mv,f.float_mv,f.pe_ttm,f.pe_dynamic,f.pb "
        "FROM stocks s LEFT JOIN fundamentals f ON f.code=s.code WHERE s.code=?", (code,))
    fin = db.query(
        "SELECT * FROM fin_report WHERE code=? ORDER BY report_date DESC LIMIT 8", (code,))
    out = df.to_dict(orient="records")
    for r in out:
        for k, v in list(r.items()):
            if isinstance(v, float) and pd.isna(v):
                r[k] = None
    return {"info": info, "financials": fin, "bars": out}


# ---------------- watch pools ----------------

class PoolName(BaseModel):
    name: str


class PoolCodes(BaseModel):
    codes: list[str]


@app.get("/api/pools")
def pools_list():
    pools = db.query("SELECT id,name,created_at FROM pools ORDER BY id")
    items = db.query(
        "SELECT p.pool_id, s.code, s.name, s.market, s.industry, "
        "f.price, f.pct_chg, f.pe_ttm, f.pb, f.total_mv, f.trade_date, "
        "(SELECT MAX(k.date) FROM kline_daily k WHERE k.code = s.code) AS kline_date "
        "FROM pool_items p JOIN stocks s ON s.code=p.code "
        "LEFT JOIN fundamentals f ON f.code=p.code ORDER BY p.added_at")
    by_pool: dict[int, list] = {p["id"]: [] for p in pools}
    for it in items:
        by_pool.setdefault(it["pool_id"], []).append(it)
    for p in pools:
        p["stocks"] = by_pool.get(p["id"], [])
    return pools


@app.post("/api/pools")
def pool_create(body: PoolName):
    db.execute("INSERT INTO pools(name) VALUES(?)", (body.name,))
    return {"id": db.query_one("SELECT last_insert_rowid() id")["id"]}


@app.put("/api/pools/{pool_id}")
def pool_rename(pool_id: int, body: PoolName):
    db.execute("UPDATE pools SET name=? WHERE id=?", (body.name, pool_id))
    return {"ok": True}


@app.delete("/api/pools/{pool_id}")
def pool_delete(pool_id: int):
    db.execute("DELETE FROM pool_items WHERE pool_id=?", (pool_id,))
    db.execute("DELETE FROM pools WHERE id=?", (pool_id,))
    return {"ok": True}


@app.post("/api/pools/{pool_id}/stocks")
def pool_add(pool_id: int, body: PoolCodes):
    for c in body.codes:
        db.execute("INSERT OR IGNORE INTO pool_items(pool_id,code) VALUES(?,?)",
                   (pool_id, c))
    return {"ok": True}


@app.delete("/api/pools/{pool_id}/stocks")
def pool_remove(pool_id: int, body: PoolCodes):
    db.execute("DELETE FROM pool_items WHERE pool_id=? AND code=?", many=[
        (pool_id, c) for c in body.codes])
    return {"ok": True}


# ---------------- screening ----------------

class ScreenReq(BaseModel):
    conditions: list[dict] = []


class AutoScreenReq(BaseModel):
    groups: list[dict] = []
    top_n: int = 100


@app.get("/api/screen/fields")
def screen_fields():
    industries = [r["industry"] for r in db.query(
        "SELECT DISTINCT industry FROM stocks WHERE industry IS NOT NULL ORDER BY industry")]
    return {
        "fundamental": screening.FUNDAMENTAL_FIELDS,
        "technical": screening.TECHNICAL_FIELDS,
        "industries": industries,
    }


@app.post("/api/screen")
def screen(body: ScreenReq):
    return {"rows": screening.screen_all(body.conditions),
            "universe": len(screening.load_universe())}


@app.post("/api/screen/auto")
def screen_auto(body: AutoScreenReq):
    weight_sum = sum(float(g.get("weight") or 0) for g in body.groups)
    if abs(weight_sum - 100) > 1e-6:
        raise HTTPException(400, f"权重之和必须为100，当前为{weight_sum}")
    return {"rows": screening.score_groups(body.groups, body.top_n)}


@app.post("/api/screen/refresh")
def screen_refresh():
    screening.load_universe(force=True)
    return {"universe": len(screening.load_universe())}


# ---------------- strategies ----------------

@app.get("/api/strategies")
def strategies_list():
    enabled = set(db.kv_get("enabled_strategies", []) or [])
    out = []
    for sid, st in discover().items():
        m = st.meta()
        m["enabled"] = sid in enabled if enabled else True
        out.append(m)
    return out


class StrategyToggle(BaseModel):
    enabled: list[str]


@app.put("/api/strategies/enabled")
def strategies_enable(body: StrategyToggle):
    valid = set(discover().keys())
    db.kv_set("enabled_strategies", [s for s in body.enabled if s in valid])
    return {"ok": True}


# ---------------- backtest ----------------

class BacktestReq(BaseModel):
    code: str
    start: str = ""
    end: str = ""
    capital: float = 1_000_000
    fee_rate: float = 0.0003
    strategy_id: str
    params: dict = {}


@app.post("/api/backtest")
def backtest_run(body: BacktestReq):
    import datetime as dt
    end = body.end or dt.date.today().isoformat()
    start = body.start or (dt.date.fromisoformat(end) - dt.timedelta(days=3650)).isoformat()
    try:
        return bt.run_backtest(body.code, start, end, body.capital,
                               body.fee_rate, body.strategy_id, body.params)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/backtest/{report_id}/excel")
def backtest_excel(report_id: str):
    report = bt.get_report(report_id)
    if report is None:
        raise HTTPException(404, "回测报告不存在或已过期，请重新回测")
    data = bt.export_excel(report)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f"attachment; filename=backtest_{report_id}.xlsx"})


# ---------------- frontend ----------------

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"),
              name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
