"""行业库存与资本周期（财报侧）：31 个申万一级行业的四象限定位。

数据来源是 `fin_capital`（东财 F10 资产负债表 + 现金流量表，由「数据中心 → F10 报表数据」
回填），样本名单 `f10_sample` 只覆盖每行业营收的一定比例（负债表档）/ 前 N（现金流档），
那套门槛在数据中心页可调，本模块按名单里记下来的实际口径显示。

四象限的两条轴都是**同期比同期**的同比：存货期末余额、资本开支累计值。现金流表给的是
年初至今累计数，同期比就不需要差分，也躲开季节性；代价是只回答「比去年同期多了还是少了」。
象限只是**描述**（库存方向 × 投资方向），不是买卖信号。

口径与偏差逐条写在 `CAVEATS`（页面「口径与偏差」直接渲染它）：核心是「同期比同期」＋
「只按两期都有数的同一批票」＋「任一列缺失的票该期整体不计入（宁可少算，不把 NULL 当 0）」＋
「两期齐的票不足 `MIN_CODES` 的行业不进象限」。
报告期不取「库里最新那期」：三季报披露中（10 月初）最新期只有百来只有数，按它算会得出
「29/31 行业成员不足」的废数 ⇒ 选期规则是「本期与去年同期都有数的票 ≥ `MIN_COVER`」的最近一期。
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from . import capital, db, industry

MIN_COVER = 0.60      # 某期要有该表名单里这么多只票有数，才当「最新可用期」
MIN_CODES = 5         # 一个行业至少几只票两期齐，才参与四象限
FLAT_PP = 1.0         # 同比落在 ±该值内算持平，不硬判方向
SERIES_MAX = 24       # 下钻序列最多给多少期

INV = ("inventory",)
CAPEX = ("capex_cash",)
CAPACITY = ("cip", "fixed_asset")
CONTRACT = ("contract_liab",)
OPERATE = ("operate_cash",)

QUADRANT = {
    ("up", "up"): "补库 + 扩产",
    ("up", "down"): "库存堆积、投放收敛",
    ("down", "down"): "去库 + 收缩",
    ("down", "up"): "去库 + 扩产",
    ("flat", "flat"): "库存与投放均持平",
    ("flat", "up"): "库存持平、投放扩张",
    ("flat", "down"): "库存持平、投放收缩",
    ("up", "flat"): "库存堆积、投放持平",
    ("down", "flat"): "库存去化、投放持平",
}
QUADRANT_DESC = {
    "补库 + 扩产": "库存和资本开支都比去年同期高：需求与投放都还在扩",
    "库存堆积、投放收敛": "库存同比在涨、资本开支在收缩：通常是需求转弱后的被动累库",
    "去库 + 收缩": "库存与资本开支同步往下：行业在出清产能",
    "去库 + 扩产": "库存往下、资本开支往上：通常是在为需求回升提前建产能",
}

CAVEATS = [
    "同比只按「两期都有数的同一批票」算：合计同比受大公司主导，逐只同比中位是等权口径，"
    "两个数不一致是正常的；基期为 0 的票算不出百分比，只进合计、不进中位。",
    "现金流量表给的是年初至今累计数，所以是同期比同期、不做差分：只回答「比去年同期多还是少」，"
    "看不出季度间的季节波动。",
    "两期都有数的票不足 5 只的行业不给同比，表里标「数据不足」，也不计入四象限家数。",
    "下钻序列里，某期该口径披露的票不足样本 60% 时不给合计与同比（否则只披露几只时会把总量算小、"
    "把同比算成断崖），四个口径都不够的期整行不给。",
    "行业归属和样本名单用的是今天的快照往回算，有幸存者与回溯偏差；这页只描述行业当前在补库还是去库、"
    "在扩产还是收缩，不构成买卖信号。",
]


def caveats(cover: float | None = None, cf_top: int | None = None) -> list[str]:
    """第一条要说清「这页的样本是按哪套门槛挑的」，所以数字取名单里记下来的那套，不写死。"""
    c, t = capital.resolve_params(cover, cf_top)
    return [f"样本不是全行业：负债表档只覆盖每行业营收 {capital.cover_text(c)}（自适应只数），"
            f"现金流档只覆盖各行业营收前 {t} 只；这套门槛在「数据中心 → F10 报表数据」页可调，"
            "这里显示的就是当前名单用的那套。银行、非银金融整体不抓（报表口径不同）。"] + CAVEATS


def _last_year(period: str) -> str:
    d = dt.date.fromisoformat(period)
    return d.replace(year=d.year - 1).isoformat()


def _load() -> dict[str, dict[str, dict[str, dict]]]:
    """level1 → code → report_date → {列: 值}。只取样本名单里的票。"""
    out: dict[str, dict[str, dict[str, dict]]] = defaultdict(lambda: defaultdict(dict))
    rows = db.query(
        "SELECT s.level1, c.code, c.report_date, c.inventory, c.cip, c.fixed_asset,"
        " c.capex_cash, c.operate_cash, c.contract_liab"
        " FROM fin_capital c JOIN f10_sample s ON s.code = c.code")
    for r in rows:
        out[r["level1"]][r["code"]][r["report_date"]] = {
            k: r[k] for k in ("inventory", "cip", "fixed_asset", "capex_cash",
                              "operate_cash", "contract_liab")}
    return dict(out)


def _period_sum(data: dict[str, dict], cols: tuple[str, ...], period: str) -> dict[str, float]:
    """code → 该期这几列合计；任一列没数的票不出现（不参与该口径的任何同比）。"""
    out = {}
    for code, per in data.items():
        row = per.get(period)
        if not row or any(row.get(c) is None for c in cols):
            continue
        out[code] = float(sum(row[c] for c in cols))
    return out


def _flat(data: dict[str, dict]) -> dict[str, dict]:
    """level1→code→period→row 摊平成 code→period→row：选期按全样本算覆盖，不分行业。"""
    return {code: per for lvl in data.values() for code, per in lvl.items()}


def _pick_period(data: dict[str, dict], col: str, denom: int) -> str | None:
    """本期和去年同期都有数只数够的最近一期；不够就往前退。"""
    if denom <= 0:
        return None
    need = max(2, int(denom * MIN_COVER + 0.999))

    def have(d: str) -> int:
        return sum(1 for per in data.values()
                   if per.get(d) and per[d].get(col) is not None)

    for d in sorted({d for per in data.values() for d, row in per.items()
                     if row.get(col) is not None}, reverse=True):
        if have(d) >= need and have(_last_year(d)) >= need:
            return d
    return None


def _yoy(cur: dict[str, float], prev: dict[str, float]) -> tuple[float | None, float | None, int]:
    """(合计同比, 逐只同比中位, 两期都有的只数)。"""
    cs = sorted(set(cur) & set(prev))
    if len(cs) < MIN_CODES:
        return None, None, len(cs)
    each = [(cur[c] - prev[c]) / abs(prev[c]) * 100 for c in cs if prev[c]]
    if len(each) < MIN_CODES:
        return None, None, len(cs)
    den = sum(prev[c] for c in cs)
    tot = None if not den else round(sum(cur[c] for c in cs) / abs(den) * 100 - 100, 1)
    each.sort()
    mid = len(each) // 2
    med = round(each[mid] if len(each) % 2 else (each[mid - 1] + each[mid]) / 2, 1)
    return tot, med, len(cs)


def _direction(v: float | None) -> str | None:
    if v is None:
        return None
    return "up" if v > FLAT_PP else "down" if v < -FLAT_PP else "flat"


def _quadrant(inv: float | None, cap: float | None) -> dict:
    di, dc = _direction(inv), _direction(cap)
    if di is None or dc is None:
        return {"quadrant": None, "label": "数据不足",
                "note": f"两期都有数的票不足 {MIN_CODES} 只，或接口未给这几列"}
    label = QUADRANT[(di, dc)]
    return {"quadrant": label, "label": label, "note": QUADRANT_DESC.get(label, "")}


def analyze() -> dict:
    data = _load()
    sample = capital.params()
    n_bal = db.query_one("SELECT COUNT(*) n FROM f10_sample WHERE need_balance=1")["n"]
    n_cash = db.query_one("SELECT COUNT(*) n FROM f10_sample WHERE need_cash=1")["n"]
    flat = _flat(data)
    bal_p = _pick_period(flat, "inventory", n_bal)
    cash_p = _pick_period(flat, "capex_cash", n_cash)
    if not bal_p and not cash_p:
        left = capital.requests_needed(capital.pending(), capital.PERIODS)
        action = (
            "到「数据中心 → F10 报表数据」点「重建名单」，再点「开始回填」"
            if not left else
            f"到「数据中心 → F10 报表数据」重建名单后点「开始回填」"
            f"（按当前门槛「{capital.cover_text(sample['cover'])} / 前 {sample['cf_top']}」"
            f"还要约 {left:,} 次请求，超过确认线要先勾选）")
        return {"ok": False, "reason": "库里还没有 F10 报表数据", "action": action,
                "levels": [], "periods": {},
                "meta": {"sample": sample, "caveats": caveats(**sample)}}
    bal_q = _last_year(bal_p) if bal_p else None
    cash_q = _last_year(cash_p) if cash_p else None

    levels = []
    for l1 in industry.level1_names():
        d = data.get(l1)
        if not d:
            continue
        codes = sorted(d)
        inv_s, inv_m, inv_n = _yoy(_period_sum(d, INV, bal_p), _period_sum(d, INV, bal_q))
        cap_s, cap_m, cap_n = _yoy(_period_sum(d, CAPEX, cash_p), _period_sum(d, CAPEX, cash_q))
        ci_s, _ci_m, ci_n = _yoy(_period_sum(d, CAPACITY, bal_p), _period_sum(d, CAPACITY, bal_q))
        cl_s, _cl_m, cl_n = _yoy(_period_sum(d, CONTRACT, bal_p), _period_sum(d, CONTRACT, bal_q))
        intensity = None
        cap_now, op_now = _period_sum(d, CAPEX, cash_p), _period_sum(d, OPERATE, cash_p)
        both = sorted(set(cap_now) & set(op_now))
        den = sum(op_now[c] for c in both)
        if len(both) >= MIN_CODES and den:
            intensity = round(sum(cap_now[c] for c in both) / abs(den) * 100, 1)
        levels.append({
            "level1": l1, "n_sample": len(codes),
            "n_inv": inv_n, "n_cap": cap_n, "n_capacity": ci_n, "n_contract": cl_n,
            "inv_yoy_sum": inv_s, "inv_yoy_med": inv_m,
            "cap_yoy_sum": cap_s, "cap_yoy_med": cap_m,
            "capacity_yoy_sum": ci_s, "contract_yoy_sum": cl_s,
            "capex_intensity": intensity,
            **_quadrant(inv_s, cap_s)})
    ready = [x for x in levels if x["quadrant"] and x["quadrant"] != "数据不足"]
    counts: dict[str, int] = defaultdict(int)
    for x in ready:
        counts[x["quadrant"]] += 1
    return {"ok": True,
            "periods": {"balance": bal_p, "balance_prev": bal_q,
                        "cash": cash_p, "cash_prev": cash_q},
            "denominator": {"n_balance": n_bal, "n_cash": n_cash},
            "meta": {"min_cover": MIN_COVER, "min_codes": MIN_CODES, "flat_pp": FLAT_PP,
                     "n_levels": len(levels), "n_ready": len(ready),
                     "sample": sample, "caveats": caveats(**sample),
                     "quadrant_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1]))},
            "levels": levels}


def _ever(data: dict[str, dict], cols: tuple[str, ...]) -> int:
    """该口径在这个行业里「至少有一期有数」的只数，当覆盖率的分母。"""
    return sum(1 for per in data.values()
               if any(all(row.get(c) is not None for c in cols) for row in per.values()))


def _coverage_ok(have: int, ever: int) -> bool:
    return have >= max(2, int(ever * MIN_COVER + 0.999))


def series(level1: str, n_periods: int = SERIES_MAX) -> dict:
    """某行业逐期序列：每期的合计（亿元）与同期同比，只按「这期和去年同期都有数」的票算。

    某期该口径披露的票不足样本 `MIN_COVER` 时，那期的合计与同比都不给（否则只披露几只时
    会把总量算小、把同比算成断崖）；四个口径都不够的期整行丢掉，所以给的是最近 N 个可用期。
    """
    data = _load().get(level1)
    if not data:
        raise ValueError(f"{level1} 在样本名单里没有数据：金融行业不参与，"
                         "或 F10 还没回填、名单还没重建")
    tags = (("inv", INV), ("capex", CAPEX), ("capacity", CAPACITY), ("contract", CONTRACT))
    ever = {tag: _ever(data, cols) for tag, cols in tags}
    dates = sorted({d for per in data.values() for d in per}, reverse=True)
    out = []
    for d in dates:
        prev = _last_year(d)
        row = {"report_date": d}
        usable = False
        for tag, cols in tags:
            cur, pre = _period_sum(data, cols, d), _period_sum(data, cols, prev)
            tot, _med, n = _yoy(cur, pre)
            paired = sorted(set(cur) & set(pre))
            ok = _coverage_ok(len(cur), ever[tag])
            usable = usable or ok
            row[f"{tag}_yi"] = round(sum(cur[c] for c in paired) / 1e8, 2) if paired and ok else None
            row[f"{tag}_yoy"] = tot if ok else None
            row[f"{tag}_n"] = n
        if usable:
            out.append(row)
    return {"level1": level1, "n_sample": len(data), "series": out[:max(1, n_periods)]}
