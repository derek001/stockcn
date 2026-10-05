"""长箱体震荡选股（box-range）：找「一波下跌出清后横着震荡 3 年以上」、现价还在箱体内合适区域的票。

复刻自视频号《老股民：不要预判下周板块轮动，真正可复用的是交易方法论》里的选股那一环
（需求文档 E:\\Project\\QoderCNProject\\.tmp-sph\\video-strategy-box-screen.md）。只做「哪些票算对的区域」，
不含买卖点、仓位、条件单——那些属于交易执行层，视频里也不是选股条件。

结构（2026-10-05 重定）：先在 6 年窗口里找最低收盘那天＝出清点，箱体只在那之后的段上算；
段内密集带给出箱顶箱底，段内收盘极值振幅承担「箱顶比箱底最多高一倍」这条形状门槛。
口径细节（带内占比恒 ≥80%，只当展示列不参与筛选）见 boxrange.py 文件头。
"""
from __future__ import annotations

from .. import screening
from .base import Selector, tradable

CROSS_MIN = 4       # 收盘穿越箱体中线至少 4 次 = 至少两个完整来回
SLOPE_FLOOR = -3.0  # 年化斜率不低于 -3%：排除「漫长的下跌中继」（视频里的 70% 错误区域）

TIERS = ("低买候选", "高卖提示", "破位观察", "突破提示")


def _tier(pos: float) -> str:
    if pos < 0:
        return "破位观察"
    if pos <= 50:
        return "低买候选"
    if pos <= 100:
        return "高卖提示"
    return "突破提示"


def _score(tier: str, pos: float) -> float:
    """分档打分：低买 60~100（越贴箱底越高）> 高卖 30~59（越靠箱顶越该收）>
    破位 15~29（跌出箱体只等收回）> 突破 0~14（追主升正是视频反对的做法），档内不重叠。"""
    if tier == "低买候选":
        return 60 + 40 * (1 - pos / 50)
    if tier == "高卖提示":
        return 30 + 29 * (pos - 50) / 50
    if tier == "破位观察":
        return 15 + 14 * max(0.0, 1 + pos / 50)
    return 14 * max(0.0, 1 - (pos - 100) / 100)


class BoxRangeSelector(Selector):
    id = "box_range"
    name = "长箱体震荡"
    description = (
        "先确认跌完了，再看是不是真横着：在最近 6 年里找到最低收盘价那一天，当作「一波下跌出清」的终点，"
        "箱体只在那一天之后算。要求之前 3 年从最高点跌得够深（默认 60%）、之后横盘满 3 年以上，"
        "并且这段行情真像一口箱子——八成收盘价挤在一条价格带里、带高够做一波差价、"
        "段内最高收盘不超过最低收盘的 2 倍（箱顶比箱底最多高一倍）、回归斜率接近水平、"
        "收盘反复穿越中线至少 4 次。命中后按现价在箱体内的位置分四档："
        "低买候选（0~50%）／高卖提示（50~100%）／破位观察（<0%）／突破提示（>100%）。"
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
        {"key": "dip_bonus", "label": "近60日挖坑后收回 加分（0=不计分）", "type": "number",
         "default": 10, "min": 0, "max": 20},
    ]

    def select(self, universe: dict[str, dict], params: dict) -> list[dict]:
        screening.ensure_box(universe)
        if params["height_min"] >= params["ext_max"]:
            raise ValueError("「密集带高度 下限」要小于「段内最高÷最低收盘 上限」，否则没有票能同时满足")
        out = []
        for code, ctx in universe.items():
            if not tradable(ctx):
                continue
            b = ctx.get("box") or {}
            height, ext = b.get("box_height"), b.get("box_ext_height")
            pos, slope, cross = b.get("box_pos"), b.get("box_slope_3y"), b.get("box_cross")
            decline, bars = b.get("box_decline_pre"), b.get("box_bars")
            if None in (height, ext, pos, slope, cross, decline, bars):
                continue
            if bars < params["seg_bars_min"] or ext > params["ext_max"]:
                continue
            if decline < params["decline_min"] or height < params["height_min"]:
                continue
            if abs(slope) > params["slope_abs_max"] or slope < SLOPE_FLOOR:
                continue
            if cross < CROSS_MIN:
                continue
            tier = _tier(pos)
            dip = bool(b.get("box_dip_60d"))
            score = _score(tier, pos)
            if dip and tier in ("低买候选", "高卖提示"):
                score = min(score + params["dip_bonus"], 100.0)
            out.append({
                "code": code,
                "score": round(score, 1),
                "reason": (f"【{tier}】现价在箱体 {pos:.1f}%、横盘 {bars / 250:.1f} 年、"
                           f"密集带宽 {height:.1f}%、段内振幅 {ext:.1f}%、"
                           f"出清回撤 {decline:.1f}%、穿越 {cross} 次、斜率 {slope:+.2f}%/年"
                           f"{'、近60日挖坑后收回' if dip else ''}"),
            })
        return out
