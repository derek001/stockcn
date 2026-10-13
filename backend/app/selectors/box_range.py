"""长箱体震荡选股（box-range）：找「一波下跌出清后横着震荡 3 年以上」、现价还在箱体内合适区域的票。

复刻自视频号《老股民：不要预判下周板块轮动，真正可复用的是交易方法论》里的选股那一环
（需求文档 E:\\Project\\QoderCNProject\\.tmp-sph\\video-strategy-box-screen.md）。只做「哪些票算对的区域」，
不含买卖点、仓位、条件单——那些属于交易执行层，视频里也不是选股条件。

结构（2026-10-05 重定）：先在 6 年窗口里找最低收盘那天＝出清点，箱体只在那之后的段上算；
段内密集带给出箱顶箱底，段内收盘极值振幅承担「箱顶比箱底最多高一倍」这条形状门槛。
口径细节（带内占比恒 ≥80%，只当展示列不参与筛选）见 boxrange.py 文件头。
2026-10-13 增强：找底带 2% 容差（新低只深一点点不再把整只票踢出池子）；箱底撞大盘 β 底的票默认剔除；
穿越次数改成按年化门槛；可选成交额/流通市值下限；档内打分叠加箱体质量（振荡密度 + 带内占比）。
"""
from __future__ import annotations

from .. import screening
from .base import Selector, tradable

SLOPE_FLOOR = -3.0  # 年化斜率不低于 -3%：排除「漫长的下跌中继」（视频里的 70% 错误区域）

TIERS = ("低买候选", "高卖提示", "破位观察", "突破提示")

# 各档分数上限：加分（挖坑/质量）只在本档内部加，保证档间不重叠（低买 > 高卖 > 破位 > 突破）
TIER_CEIL = {"低买候选": 100.0, "高卖提示": 59.0, "破位观察": 29.0, "突破提示": 14.0}


def _tier(pos: float) -> str:
    if pos < 0:
        return "破位观察"
    if pos <= 50:
        return "低买候选"
    if pos <= 100:
        return "高卖提示"
    return "突破提示"


def _score(tier: str, pos: float) -> float:
    """位置基础分：低买 60~100（越贴箱底越高）> 高卖 30~59（越靠箱顶越该收）>
    破位 15~29（跌出箱体只等收回）> 突破 0~14（追主升正是视频反对的做法），档内不重叠。"""
    if tier == "低买候选":
        return 60 + 40 * (1 - pos / 50)
    if tier == "高卖提示":
        return 30 + 29 * (pos - 50) / 50
    if tier == "破位观察":
        return 15 + 14 * max(0.0, 1 + pos / 50)
    return 14 * max(0.0, 1 - (pos - 100) / 100)


def _quality(cross_year: float, time_pct: float | None) -> float:
    """箱体质量分（0~10，只加给低买/高卖两档）：

    振荡密度 0~6 分：穿越中线 ≥4 次/年 给满——来回越勤，高抛低吸越有的做；
    带内占比 0~4 分：time_pct 80%（密集带定义的下界）→95%+ 线性给满——价格越贴着带走，箱子越「真」。
    权重是先验，回放校准（boxreplay.py）可以检验这两项到底值几分。"""
    osc = 6.0 * min(cross_year / 4.0, 1.0)
    tight = 0.0
    if time_pct is not None:
        tight = 4.0 * max(0.0, min((time_pct - 80.0) / 15.0, 1.0))
    return osc + tight


def gates_ok(b: dict, p: dict) -> tuple[bool, float]:
    """纯箱体字段的七条门槛，选股器与回放校准（boxreplay.py）共用同一份口径。

    p 要先过 resolve_params 补全默认值。返回 (是否命中, 穿越中线次数/年)；
    不含流动性/ST/当日新K线这些依赖 universe 快照的条件——那些只有选股器能查。"""
    height, ext = b.get("box_height"), b.get("box_ext_height")
    pos, slope, cross = b.get("box_pos"), b.get("box_slope_3y"), b.get("box_cross")
    decline, bars = b.get("box_decline_pre"), b.get("box_bars")
    if None in (height, ext, pos, slope, cross, decline, bars) or not bars:
        return False, 0.0
    if bars < p["seg_bars_min"] or ext > p["ext_max"]:
        return False, 0.0
    if decline < p["decline_min"] or height < p["height_min"]:
        return False, 0.0
    if abs(slope) > p["slope_abs_max"] or slope < SLOPE_FLOOR:
        return False, 0.0
    cross_year = cross * 250.0 / bars
    if cross_year < p["cross_min_per_year"]:
        return False, 0.0
    if b.get("box_beta_bottom") and p["beta_exclude"] >= 0.5:
        return False, 0.0
    return True, cross_year


class BoxRangeSelector(Selector):
    id = "box_range"
    name = "长箱体震荡"
    description = (
        "先确认跌完了，再看是不是真横着：在最近 6 年里找到最低收盘价那一天（收盘不高于最低×1.02 的最早一天，"
        "带容差是为了新低只深一点点时锚点不乱跳），当作「一波下跌出清」的终点，箱体只在那一天之后算。"
        "要求之前 3 年从最高点跌得够深（默认 60%）、之后横盘满 3 年以上，"
        "并且这段行情真像一口箱子——八成收盘价挤在一条价格带里、带高够做一波差价、"
        "段内最高收盘不超过最低收盘的 2 倍（箱顶比箱底最多高一倍）、回归斜率接近水平、"
        "收盘穿越中线至少每年 1.3 次（默认，折算自旧的「3 年 4 次」）。"
        "箱底日期撞上大盘指数自身最低（±20 个交易日）的票默认剔除——那是大盘 β 底，不是个股出清"
        "（想留着对比就把「β底剔除」改成 0，名单里会打标记）。"
        "可选当日成交额/流通市值下限（默认 0=不限），防小票困境股。"
        "命中后按现价在箱体内的位置分四档：低买候选（0~50%）／高卖提示（50~100%）／破位观察（<0%）／"
        "突破提示（>100%）；低买/高卖两档的分数还会叠加箱体质量（穿越频率、带内占比）与挖坑加分。"
        "要读满 6 年日线，第一次跑约 2~3 分钟（股票池 1 分钟 + 全市场箱体 1 分钟），之后同一个后端进程里再跑直接命中缓存；"
        "同一时刻只能跑一个全市场任务，别的页面正在筛选或回测时会排队等。剔除 ST 和当日没有新K线的票。")
    params_schema = [
        {"key": "ext_max", "label": "段内最高÷最低收盘 上限(%)", "type": "number",
         "default": 100, "min": 40, "max": 200},
        {"key": "decline_min", "label": "出清回撤 下限(%)", "type": "number",
         "default": 60, "min": 0, "max": 90},
        {"key": "seg_bars_min", "label": "箱体横盘 至少(个交易日)", "type": "number",
         "default": 750, "min": 375, "max": 1500},
        {"key": "height_min", "label": "密集带高度 下限(%)", "type": "number",
         "default": 15, "min": 0, "max": 60},
        {"key": "slope_abs_max", "label": "年化斜率 绝对值上限(%)", "type": "number",
         "default": 5, "min": 0.5, "max": 20},
        {"key": "cross_min_per_year", "label": "穿越中线 至少(次/年)", "type": "number",
         "default": 1.3, "min": 0, "max": 10},
        {"key": "dip_bonus", "label": "近60日挖坑后收回 加分（0=不计分）", "type": "number",
         "default": 10, "min": 0, "max": 20},
        {"key": "amount_min", "label": "当日成交额 下限(亿, 0=不限)", "type": "number",
         "default": 0, "min": 0, "max": 100},
        {"key": "float_mv_min", "label": "流通市值 下限(亿, 0=不限)", "type": "number",
         "default": 0, "min": 0, "max": 500},
        {"key": "beta_exclude", "label": "箱底撞大盘β底(1=剔除 0=保留标记)", "type": "number",
         "default": 1, "min": 0, "max": 1},
    ]

    def select(self, universe: dict[str, dict], params: dict) -> list[dict]:
        screening.ensure_box(universe)
        if params["height_min"] >= params["ext_max"]:
            raise ValueError("「密集带高度 下限」要小于「段内最高÷最低收盘 上限」，否则没有票能同时满足")
        out = []
        for code, ctx in universe.items():
            if not tradable(ctx):
                continue
            if params["amount_min"] > 0 and (ctx.get("amount") or 0) < params["amount_min"]:
                continue
            if params["float_mv_min"] > 0 and (ctx.get("float_mv") or 0) < params["float_mv_min"]:
                continue
            b = ctx.get("box") or {}
            ok, cross_year = gates_ok(b, params)
            if not ok:
                continue
            height, ext = b["box_height"], b["box_ext_height"]
            pos, slope, cross, decline, bars = (
                b["box_pos"], b["box_slope_3y"], b["box_cross"], b["box_decline_pre"], b["box_bars"])
            beta = bool(b.get("box_beta_bottom"))
            tier = _tier(pos)
            dip = bool(b.get("box_dip_60d"))
            score = _score(tier, pos)
            if tier in ("低买候选", "高卖提示"):
                score += _quality(cross_year, b.get("box_time_pct"))
                if dip:
                    score += params["dip_bonus"]
            score = min(score, TIER_CEIL[tier])
            out.append({
                "code": code,
                "score": round(score, 1),
                "reason": (f"【{tier}】现价在箱体 {pos:.1f}%、横盘 {bars / 250:.1f} 年、"
                           f"密集带宽 {height:.1f}%、段内振幅 {ext:.1f}%、"
                           f"出清回撤 {decline:.1f}%、穿越 {cross} 次（{cross_year:.1f}/年）、"
                           f"斜率 {slope:+.2f}%/年"
                           f"{'、近60日挖坑后收回' if dip else ''}"
                           f"{'、箱底撞大盘β底' if beta else ''}"),
            })
        return out
