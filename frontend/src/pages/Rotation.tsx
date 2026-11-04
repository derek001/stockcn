import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import * as echarts from "echarts";
import { api } from "../api";

// ---------- 与 backend/app/rotation.py 的返回结构一一对应 ----------

interface Chg { "1m": number | null; "3m": number | null; "6m": number | null; "12m": number | null }
interface StrengthRow {
  level1: string; chg: Chg; ytd_pct: number | null; excess_3m: number | null;
  pos_1y_pct: number | null; dd_1y_pct: number | null;
  above_ma20: boolean; above_ma60: boolean | null;
  amt_share_pct: number | null; amt_share_pctile: number | null;
  rank_1m?: number; rank_3m?: number; rank_6m?: number; rank_12m?: number;
}
interface GroupSeries { dates: string[]; a_level: (number | null)[]; b_level: (number | null)[]; spread: (number | null)[] }
interface BenchSeries { dates: string[]; bench: (number | null)[] }
interface MainSeries { dates: string[]; mainline: (number | null)[]; bench: (number | null)[]; conc: (number | null)[] }
interface Loading { level1: string; loading: number; group: "A" | "B" | "-"; loading_full: number }
interface Stability { label: string; corr_with_full: number; explained_pct: number }
interface Spread {
  note: string; cur_month_pct: number | null; last3_pct: number | null;
  month_mean_abs_pct: number | null; month_std_pct: number | null;
  adj_month_corr: number | null; cum: number | null; cum_pctile: number | null;
  roll_z: number | null; series: GroupSeries;
}
interface Reversion {
  ic_1m: number | null; t_1m: number | null; n_1m: number;
  ic_3m: number | null; t_3m: number | null; n_3m: number;
  hi_mean_pct: number | null; hi_n: number; lo_mean_pct: number | null; lo_n: number;
  note: string;
}
interface Families {
  available: boolean; reason?: string; method?: string; months_tested?: number;
  current_month?: string; explained_pct?: number; full_sample_explained_pct?: number;
  explained_pct_median?: number; group_a?: string[]; group_b?: string[];
  loadings?: Loading[]; flip_avg?: number | null; flip_max?: number | null;
  intra_group_corr?: { a: number | null; b: number | null } | null;
  overlap_with_full?: { a: string; b: string }; stability?: Stability[];
  spread?: Spread; reversion?: Reversion;
}
interface Dispersion {
  unit: string; current_20d: number; pctile: number | null;
  p10: number; p50: number; p90: number;
  monthly: { date: string; val: number }[]; note: string;
}
interface IndexBox {
  code: string; name: string; date: string; newer_bar_date: string | null; stale_note: string | null;
  close: number; chg_1d: number; ytd_pct: number; ytd_date: string;
  box_lo: number; box_hi: number; box_pos_pct: number | null; box_width_pct: number | null;
  chg_12m: number | null; above_ma20: boolean; ma60: number | null; above_ma60: boolean | null;
}
interface Breadth {
  stocks_in_pool: number; stocks_with_last_bar: number;
  ytd_base_date: string; ytd_n: number; ytd_median_pct: number | null; ytd_mean_pct: number | null;
  ytd_down_share_pct: number | null; ytd_down_over_20_n: number | null;
  window_hi_reach_n: number; window_hi_reach_pct: number | null; at_window_high_n: number;
  window_lo_reach_n: number; window_lo_reach_pct: number | null;
  adv_n: number; dec_n: number; flat_n: number;
  above_ma20_pct: number | null; above_ma60_pct: number | null; window_years_note: string;
}
interface Leader {
  level1: string; chg_1m: number | null; chg_3m: number | null; chg_6m: number | null;
  excess_3m: number | null; amt_share_pct: number | null; pos_1y_pct: number | null;
}
interface Mainline {
  top_n: number; leaders: Leader[]; state: string; state_label: string;
  concentration_pct: number; concentration_pctile: number | null;
  concentration_chg_20d_pp: number | null; mainline_dd_1y_pct: number;
  leader_1m_mean_pct: number | null; others_1m_mean_pct: number | null;
  leader_3m_excess_mean_pct: number | null; catchup_gap_pp: number | null;
  above_ma60_n: number; momentum_leaders: string[];
  evidence: string[]; rules: Record<string, string>; series: MainSeries;
}
interface RotationResp {
  meta: {
    as_of: string; window_start: string; window_end: string; years: number; bars: number;
    top_n: number; industries: number;
    industry_members: { level1: string; n_members: number; n_member_codes: number }[];
    unclassified_stocks: number; price_note: string; caveats: string[]; calc_sec: number;
  };
  market: { index: IndexBox; breadth: Breadth };
  strength: StrengthRow[];
  benchmark: { name: string; base: number; chg: Chg; bench_3m_pct: number | null };
  benchmark_series: BenchSeries;
  dispersion: Dispersion;
  families: Families;
  mainline: Mainline;
}

const INDEX_OPTIONS = [
  { code: "sh000001", name: "上证指数" },
  { code: "sz399001", name: "深证成指" },
  { code: "sz399006", name: "创业板指" },
  { code: "sh000688", name: "科创50" },
];
const C_UP = "#ef5350", C_DOWN = "#26a69a", C_LINE = "#4f8cff", C_A = "#ef5350", C_B = "#26a69a",
  C_CONC = "#f7b500";

const fp = (v: number | null | undefined) => (v == null ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);
const fs = (v: number | null | undefined, unit = "pp") => (v == null ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(1)}${unit}`);
const pctCls = (v: number | null | undefined) =>
  v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";

// 强弱热力格：正红负绿，深浅按绝对值（30% 封顶）
const heatStyle = (v: number | null): CSSProperties => {
  if (v == null) return {};
  const a = Math.min(Math.abs(v) / 30, 1) * 0.5 + 0.06;
  return { background: v > 0 ? `rgba(239,83,80,${a.toFixed(2)})` : `rgba(38,166,154,${a.toFixed(2)})` };
};

// ---------- 通用多轴折线（本页专用：族净值/价差、主线/抱团度、离散度） ----------

interface LineDef { name: string; data: (number | null)[]; color: string; yAxis?: number; bar?: boolean }

function MultiChart({ dates, lines, height = 300, yNames = [""] }:
  { dates: string[]; lines: LineDef[]; height?: number; yNames?: string[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    chart.setOption({
      backgroundColor: "transparent", animation: false,
      tooltip: { trigger: "axis" },
      legend: { textStyle: { color: "#c8d0dc", fontSize: 11 }, top: 0 },
      grid: { left: 58, right: yNames.length > 1 ? 62 : 24, top: 30, bottom: 46 },
      xAxis: { type: "category", data: dates },
      yAxis: yNames.map((n, i) => ({
        type: "value", name: n, scale: true,
        nameTextStyle: { color: "#8a94a6", fontSize: 10 },
        axisLabel: { color: "#8a94a6", fontSize: 10 },
        splitLine: { lineStyle: { color: i === 0 ? "rgba(138,148,166,0.15)" : "transparent" } },
      })),
      dataZoom: [{ type: "inside" }, { type: "slider", height: 18, bottom: 8 }],
      series: lines.map((l) => ({
        name: l.name, type: l.bar ? "bar" : "line", data: l.data,
        yAxisIndex: l.yAxis ?? 0, showSymbol: false,
        lineStyle: { width: 1.4, color: l.color },
        itemStyle: { color: l.color },
      })),
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => { window.removeEventListener("resize", onResize); ro.disconnect(); chart.dispose(); };
  }, [dates, lines, height, yNames]);
  return <div ref={ref} className="chart" style={{ height }} />;
}

// ---------- 主页面 ----------

export default function Rotation() {
  const [indexCode, setIndexCode] = useState("sh000001");
  const [years, setYears] = useState(5);
  const [topN, setTopN] = useState(5);
  const [data, setData] = useState<RotationResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [showCaveat, setShowCaveat] = useState(false);
  const [sortKey, setSortKey] = useState<"3m" | "1m" | "amt">("3m");

  const load = useCallback(() => {
    setBusy(true);
    setErr("");
    const q = new URLSearchParams({ index: indexCode, years: String(years), top_n: String(topN) });
    api.get<RotationResp>(`/api/rotation?${q}`).then((r) => setData(r))
      .catch((e) => { setErr(e.message); setData(null); })
      .finally(() => setBusy(false));
  }, [indexCode, years, topN]);

  useEffect(() => { load(); }, [load]);

  const fam = data?.families;
  const ml = data?.mainline;

  const sortedStrength = useMemo(() => {
    if (!data) return [];
    const key = sortKey === "amt" ? "amt_share_pct" : sortKey;
    const val = (r: StrengthRow) =>
      key === "amt_share_pct" ? r.amt_share_pct : r.chg[key as "1m" | "3m"];
    return [...data.strength].sort((a, b) => (val(b) ?? -1e9) - (val(a) ?? -1e9));
  }, [data, sortKey]);

  const stateKind = (s: string) =>
    s === "crowded" ? "up" : s === "broken" ? "down" : s === "spread" ? "" : "";

  return (
    <>
      <div className="panel">
        <div className="row">
          <h3 style={{ margin: 0 }}>板块轮动</h3>
          <span className="muted">
            申万一级 {data?.meta.industries ?? 31} 个行业｜等权复权净值｜只做「现在处于哪一段」的阶段识别，不预测下个月哪个板块强
          </span>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <span className="muted">参照指数</span>
          <select value={indexCode} onChange={(e) => setIndexCode(e.target.value)}>
            {INDEX_OPTIONS.map((o) => <option key={o.code} value={o.code}>{o.name}</option>)}
          </select>
          <span className="muted">分析窗口</span>
          <select value={years} onChange={(e) => setYears(Number(e.target.value))}>
            {[3, 5, 8, 10].map((v) => <option key={v} value={v}>近 {v} 年</option>)}
          </select>
          <span className="muted">主线行业数</span>
          <select value={topN} onChange={(e) => setTopN(Number(e.target.value))}>
            {[3, 5, 8].map((v) => <option key={v} value={v}>{v} 个</option>)}
          </select>
          <button className="btn" onClick={load} disabled={busy}>重算</button>
          {busy && <span className="muted">全市场重算中，约 30~50 秒…</span>}
          {data && !busy && <span className="muted">基准日 {data.meta.as_of} · 用时 {data.meta.calc_sec}s</span>}
        </div>
        {data && (
          <div className="row" style={{ marginTop: 8 }}>
            <span className="badge">{data.meta.window_start} ~ {data.meta.window_end}</span>
            <span className="muted">
              {data.meta.bars} 个交易日 · {data.meta.industries} 个一级行业 ·
              未分类股票 {data.meta.unclassified_stocks} 只（不进行业统计） · {data.meta.price_note}
            </span>
            <button className="btn" style={{ padding: "2px 8px" }}
              onClick={() => setShowCaveat((v) => !v)}>
              {showCaveat ? "收起口径与偏差" : "口径与偏差"}
            </button>
          </div>
        )}
        {showCaveat && data && (
          <ul className="muted" style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 13 }}>
            {data.meta.caveats.map((c, i) => <li key={i}>{c}</li>)}
            <li>参照指数只用于箱体与涨跌读数，行业统计与它无关；{data.market.index.stale_note ?? "指数与个股末根同日"}</li>
          </ul>
        )}
        {err && <div className="up" style={{ marginTop: 8 }}>{err}</div>}
        {!data && !busy && !err && <p className="muted">点「重算」开始。</p>}
      </div>

      {data && (
        <>
          {/* 三张状态卡 */}
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>市场结构 · 指数箱体与宽度</h3>
              <div className="row" style={{ marginBottom: 6 }}>
                <span className={`badge ${data.market.index.box_pos_pct != null && data.market.index.box_pos_pct > 60 ? "up" : data.market.index.box_pos_pct != null && data.market.index.box_pos_pct < 40 ? "down" : ""}`}
                  style={{ fontSize: 13 }}>
                  箱体位置 {fmtPos(data.market.index.box_pos_pct)}
                </span>
                <span className="muted">
                  {data.market.index.name} {data.market.index.close.toFixed(0)}（日内 {fp(data.market.index.chg_1d)}）
                </span>
              </div>
              <ul className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                <li>近一年箱体 {data.market.index.box_lo.toFixed(0)} ~ {data.market.index.box_hi.toFixed(0)}
                  （振幅 {data.market.index.box_width_pct?.toFixed(1)}%），YTD {fp(data.market.index.ytd_pct)}，
                  12 个月 {fp(data.market.index.chg_12m)}</li>
                <li>站上 MA20 {data.market.index.above_ma20 ? "是" : "否"}；MA60 {data.market.index.ma60?.toFixed(0) ?? "-"}
                  （{data.market.index.above_ma60 == null ? "不可比" : data.market.index.above_ma60 ? "在其上方" : "在其下方"}）</li>
                <li>个股宽度（YTD，复权收盘）：中位 {fp(data.market.breadth.ytd_median_pct)}，
                  下跌占比 {data.market.breadth.ytd_down_share_pct?.toFixed(0)}%，
                  跌超 20% 的 {data.market.breadth.ytd_down_over_20_n} 只（样本 {data.market.breadth.ytd_n} 只）</li>
                <li>{data.market.breadth.window_years_note}：年内创窗口新高 {data.market.breadth.window_hi_reach_n} 只
                  （{data.market.breadth.window_hi_reach_pct?.toFixed(0)}%），新低 {data.market.breadth.window_lo_reach_n} 只
                  （{data.market.breadth.window_lo_reach_pct?.toFixed(0)}%）</li>
                <li>末根涨跌 {data.market.breadth.adv_n}/{data.market.breadth.dec_n} 只，
                  站上 MA20 占比 {data.market.breadth.above_ma20_pct?.toFixed(0)}%，
                  MA60 {data.market.breadth.above_ma60_pct?.toFixed(0)}%</li>
              </ul>
            </div>

            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>板块族 · 一起动的是哪一撮</h3>
              {fam?.available ? (
                <>
                  <div className="row" style={{ marginBottom: 6 }}>
                    <span className="badge" style={{ fontSize: 13 }}>
                      残差轴解释率 {fam.explained_pct}%（31 个行业随机只有 3%）
                    </span>
                    <span className="muted">已因果重算 {fam.months_tested} 个月</span>
                  </div>
                  <ul className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                    <li><span className="up">A 族（轴正端）</span>：{fam.group_a?.join("、")}</li>
                    <li><span className="down">B 族（轴负端）</span>：{fam.group_b?.join("、")}</li>
                    <li>族价差累计 {fs(fam.spread?.cum, "%")}，窗口内分位 {fam.spread?.cum_pctile?.toFixed(0)}%，
                      滚动 {fam.spread?.roll_z == null ? "z 值样本不足" : `3 年 z ${fam.spread.roll_z.toFixed(2)}`}</li>
                    <li>本月族价差 {fs(fam.spread?.cur_month_pct, "%")}，近 3 月累计 {fs(fam.spread?.last3_pct, "%")}</li>
                    <li>轴稳不稳：分段载荷与全样本相关 {(fam.stability ?? []).map((s) => `${s.label} ${s.corr_with_full.toFixed(2)}`).join(" / ")}；
                      每月换组平均 {fam.flip_avg ?? "-"} 个行业</li>
                    <li>组内一致性：A {fam.intra_group_corr?.a ?? "-"}，B {fam.intra_group_corr?.b ?? "-"}（两两相关均值）</li>
                  </ul>
                </>
              ) : <p className="muted">{fam?.reason ?? "样本不足"}</p>}
            </div>

            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>主线生命周期</h3>
              {ml && (
                <>
                  <div className="row" style={{ marginBottom: 6 }}>
                    <span className={`badge ${stateKind(ml.state)}`} style={{ fontSize: 13 }}>{ml.state_label}</span>
                    <span className="muted">抱团度 {ml.concentration_pct.toFixed(1)}%
                      （分位 {ml.concentration_pctile?.toFixed(0)}%）</span>
                  </div>
                  <ul className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                    <li><span className="up">钱在哪</span>：{ml.leaders.map((l) => l.level1).join("、")}</li>
                    <li><span className="muted">涨在哪</span>：{ml.momentum_leaders.join("、")}</li>
                    <li>主线 1M 均值 {fp(ml.leader_1m_mean_pct)} vs 其余行业 {fp(ml.others_1m_mean_pct)}，
                      接力差 {fs(ml.catchup_gap_pp)}</li>
                    <li>主线距一年高点 {fp(ml.mainline_dd_1y_pct)}，抱团度 20 日 {fs(ml.concentration_chg_20d_pp, "pp")}，
                      站上 MA60 的 {ml.above_ma60_n}/{ml.top_n} 个</li>
                  </ul>
                </>
              )}
            </div>
          </div>

          {/* 主线生命周期图 */}
          {ml && (
            <div className="panel">
              <h3>主线（成交占比前 {ml.top_n}）vs 31 行业等权 · 下为抱团度</h3>
              <MultiChart
                dates={ml.series.dates} height={330} yNames={["净值(起点100)", "成交占比%"]}
                lines={[
                  { name: "主线", data: ml.series.mainline, color: C_UP },
                  { name: "31 行业等权", data: ml.series.bench, color: C_LINE },
                  { name: "抱团度(20日均)", data: ml.series.conc, color: C_CONC, yAxis: 1 },
                ]}
              />
              <ul className="muted" style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 13 }}>
                {ml.evidence.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
              <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
                判定规则：{Object.entries(ml.rules).map(([k, v]) => `${k}＝${v}`).join("；")}
              </div>
            </div>
          )}

          {/* 强弱矩阵 */}
          <div className="panel">
            <div className="row">
              <h3 style={{ margin: 0 }}>行业强弱矩阵（截面离散度 {data.dispersion.current_20d.toFixed(2)}%，窗口分位 {data.dispersion.pctile?.toFixed(0)}%）</h3>
              <span className="muted">排序</span>
              <select value={sortKey} onChange={(e) => setSortKey(e.target.value as "3m" | "1m" | "amt")}>
                <option value="3m">近 3 月</option>
                <option value="1m">近 1 月</option>
                <option value="amt">成交占比</option>
              </select>
              <span className="muted">大盘基准（{data.benchmark.name}）1M {fp(data.benchmark.chg["1m"])} · 3M {fp(data.benchmark.chg["3m"])} · 12M {fp(data.benchmark.chg["12m"])}</span>
            </div>
            <div className="table-wrap" style={{ maxHeight: 520, marginTop: 10 }}>
              <table>
                <thead><tr>
                  <th className="l">行业</th><th>1M</th><th>3M</th><th>6M</th><th>12M</th><th>YTD</th>
                  <th>3M超额</th><th>1M名</th><th>3M名</th><th>一年位置</th><th>距一年高点</th>
                  <th>成交占比</th><th>占比分位</th><th>MA20/60</th>
                </tr></thead>
                <tbody>
                  {sortedStrength.map((r) => (
                    <tr key={r.level1}>
                      <td className="l">{r.level1}</td>
                      <td className={pctCls(r.chg["1m"])} style={heatStyle(r.chg["1m"])}>{fp(r.chg["1m"])}</td>
                      <td className={pctCls(r.chg["3m"])} style={heatStyle(r.chg["3m"])}>{fp(r.chg["3m"])}</td>
                      <td className={pctCls(r.chg["6m"])} style={heatStyle(r.chg["6m"])}>{fp(r.chg["6m"])}</td>
                      <td className={pctCls(r.chg["12m"])} style={heatStyle(r.chg["12m"])}>{fp(r.chg["12m"])}</td>
                      <td className={pctCls(r.ytd_pct)}>{fp(r.ytd_pct)}</td>
                      <td className={pctCls(r.excess_3m)}>{fs(r.excess_3m, "pp")}</td>
                      <td>{r.rank_1m ?? "-"}</td><td>{r.rank_3m ?? "-"}</td>
                      <td>{r.pos_1y_pct == null ? "-" : `${r.pos_1y_pct.toFixed(0)}%`}</td>
                      <td className={pctCls(r.dd_1y_pct)}>{fp(r.dd_1y_pct)}</td>
                      <td>{r.amt_share_pct == null ? "-" : `${r.amt_share_pct.toFixed(2)}%`}</td>
                      <td>{r.amt_share_pctile == null ? "-" : `${r.amt_share_pctile.toFixed(0)}%`}</td>
                      <td className="muted">
                        {r.above_ma20 ? "20↑" : "20↓"}{r.above_ma60 == null ? "" : r.above_ma60 ? " 60↑" : " 60↓"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
              成交占比＝该行业成分股成交额 / 31 个行业成分股成交额合计（20 日均）；分位是它在分析窗口内的位置。
              {data.dispersion.note}
            </div>
          </div>

          {/* 族轴 */}
          {fam?.available && fam.spread && (
            <div className="panel">
              <h3>板块族净值与价差</h3>
              <MultiChart
                dates={fam.spread.series.dates} height={300} yNames={["净值(起点100)", "A-B价差%"]}
                lines={[
                  { name: "A 族", data: fam.spread.series.a_level, color: C_A },
                  { name: "B 族", data: fam.spread.series.b_level, color: C_B },
                  { name: "A/B 价差", data: fam.spread.series.spread, color: C_CONC, yAxis: 1 },
                ]}
              />
              <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
                分组口径：{fam.method}。
                {fam.spread.note}；月价差绝对值均值 {fs(fam.spread.month_mean_abs_pct, "%")}、
                标准差 {fs(fam.spread.month_std_pct, "%")}、相邻月相关 {fam.spread.adj_month_corr ?? "-"}
                （负＝这个月冲了下个月倾向回落）。
              </div>
              {fam.reversion && (
                <div className="muted" style={{ marginTop: 4, fontSize: 12 }}>
                  历史检验（因果口径，只用当月之前的信息）：分位 vs 未来 1 月价差 IC {fam.reversion.ic_1m ?? "-"}
                  （t={fam.reversion.t_1m ?? "-"}，n={fam.reversion.n_1m}），vs 未来 3 月 IC {fam.reversion.ic_3m ?? "-"}
                  （t={fam.reversion.t_3m ?? "-"}，n={fam.reversion.n_3m}）；
                  分位&gt;80% 的后 1 月均值 {fs(fam.reversion.hi_mean_pct, "%")}（n={fam.reversion.hi_n}），
                  分位&lt;20% 的 {fs(fam.reversion.lo_mean_pct, "%")}（n={fam.reversion.lo_n}）。
                  {fam.reversion.note}
                </div>
              )}
            </div>
          )}

          {/* 载荷表 + 离散度 */}
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))" }}>
            {fam?.available && (
              <div className="panel" style={{ marginBottom: 0 }}>
                <h3>族载荷（当月因果版 vs 全样本前视版）</h3>
                <div className="table-wrap" style={{ maxHeight: 380 }}>
                  <table>
                    <thead><tr><th className="l">行业</th><th>因果载荷</th><th>组</th><th>前视载荷</th></tr></thead>
                    <tbody>
                      {(fam.loadings ?? []).map((l) => (
                        <tr key={l.level1}>
                          <td className="l">{l.level1}</td>
                          <td className={pctCls(l.loading)}>{l.loading.toFixed(3)}</td>
                          <td className={l.group === "A" ? "up" : l.group === "B" ? "down" : "muted"}>{l.group}</td>
                          <td className="muted">{l.loading_full.toFixed(3)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
                  因果版与全样本版分组重合度 A {fam.overlap_with_full?.a}、B {fam.overlap_with_full?.b}；
                  重合高说明「族」这件事不是靠后视拼出来的。
                </div>
              </div>
            )}
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>截面离散度（月度，{data.dispersion.unit}）</h3>
              <MultiChart
                dates={data.dispersion.monthly.map((m) => m.date)} height={300} yNames={["%"]}
                lines={[{ name: "月度截面标准差", data: data.dispersion.monthly.map((m) => m.val), color: C_LINE, bar: true }]}
              />
              <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
                窗口内 P10 {data.dispersion.p10.toFixed(2)} / P50 {data.dispersion.p50.toFixed(2)} / P90 {data.dispersion.p90.toFixed(2)}；
                当前 20 日均 {data.dispersion.current_20d.toFixed(2)}（分位 {data.dispersion.pctile?.toFixed(0)}%）。
                离散度低的时候「轮动」这件事本身没有空间。
              </div>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function fmtPos(v: number | null | undefined): string {
  if (v == null) return "-";
  if (v >= 80) return `${v.toFixed(0)}%（近箱体顶）`;
  if (v <= 20) return `${v.toFixed(0)}%（近箱体底）`;
  return `${v.toFixed(0)}%（箱体内）`;
}
