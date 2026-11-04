import { useCallback, useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import { api } from "../api";

// ---------- 与 backend/app/cycles.py 的返回结构一一对应 ----------

interface Pivot { date: string; price: number; type: "top" | "bottom"; }
interface Provisional { date: string; price: number; type: string; retrace_pct: number; }
interface Round {
  kind: "bull" | "bear"; kind_label: string; label: string;
  from_date: string; from_price: number; from_idx: number;
  to_date: string; to_price: number; to_idx: number;
  chg_pct: number; months: number;
}
interface Current {
  kind: "bull" | "bear"; kind_label: string;
  from_date: string; from_price: number; start_idx: number;
  months: number; chg_pct: number;
  seg_hi: number; seg_hi_date: string; seg_lo: number; seg_lo_date: string;
  drawdown_pct?: number; rebound_pct?: number;
  state: string; confirm_gap_pct?: number;
}
interface ShortPanel {
  window: number; partial: boolean; swing: number;
  state: "up" | "down" | "range"; state_label: string;
  evidence: string[];
  hi: number; hi_date: string; lo: number; lo_date: string; pos_pct: number;
  ma20: number | null; ma60: number | null;
  pivots: Pivot[]; provisional: Provisional | null;
}
interface SeasonRow { month: number; n: number; mean_pct: number | null; median_pct: number | null; win_rate: number | null; }
interface AnaCase {
  label: string; at_date: string; at_price: number;
  phase_chg_pct: number; round_chg_pct: number;
  fwd3: number | null; fwd6: number | null; fwd12: number | null;
}
interface FwdStat { n: number; win_rate: number; p25: number; p50: number; p75: number; }
interface Analogy {
  phase: { kind: string; kind_label: string; months: number; chg_pct: number; from_date: string };
  cases: AnaCase[];
  ended_before: { label: string; months: number; chg_pct: number }[];
  stats: Record<string, FwdStat>;
  note: string | null;
}
interface LongPanel {
  pctile: number;
  last_top: { date: string; price: number } | null;
  last_bottom: { date: string; price: number } | null;
  dist_last_top_pct: number | null; dist_last_bottom_pct: number | null;
  rounds_count: number; bull_count: number; bear_count: number;
  bull_med_chg_pct: number | null; bear_med_chg_pct: number | null;
  bull_med_months: number | null; bear_med_months: number | null;
  decades: number[]; current_kind: string | null;
}
interface CyclesResp {
  meta: {
    subject: "index" | "stock"; index_code: string | null; code: string | null;
    name: string; price_note: string; swing: number; short_swing: number;
    bars: number; first_date: string; last_date: string;
    skipped_factor_rows: number; rounds_note: string;
  };
  dates: string[]; closes: number[];
  pivots: Pivot[]; provisional: Provisional | null;
  rounds: Round[]; current: Current | null;
  short: ShortPanel; seasonality: SeasonRow[];
  analogy: Analogy | null; long: LongPanel;
}

const INDEX_OPTIONS = [
  { code: "sh000001", name: "上证指数" },
  { code: "sz399001", name: "深证成指" },
  { code: "sz399006", name: "创业板指" },
];
const C_UP = "#ef5350", C_DOWN = "#26a69a", C_LINE = "#4f8cff", C_MA20 = "#f7b500", C_MA60 = "#e15bef";
const pctCls = (v: number | null | undefined) => (v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");
const fp = (v: number | null | undefined) => (v == null ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);

// ---------- 通用折线图（周期页专用：牛熊着色 / 转折点标记 / 十年网格 / 对数轴） ----------

interface LineProps {
  dates: string[]; data: (number | null)[]; height?: number;
  extraLines?: { name: string; data: (number | null)[]; color: string; dashed?: boolean }[];
  markAreas?: { from: string; to: string; color: string }[];
  markPoints?: { date: string; price: number; color: string; symbol: string; label: string }[];
  vLines?: { date: string; label: string }[];
  logY?: boolean; zoom?: boolean;
}

function CycleLineChart({ dates, data, height = 300, extraLines = [], markAreas = [],
  markPoints = [], vLines = [], logY = false, zoom = false }: LineProps) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    const series: echarts.SeriesOption[] = [{
      name: "收盘", type: "line", data, showSymbol: false,
      lineStyle: { width: 1.4, color: C_LINE },
      markArea: markAreas.length ? {
        silent: true,
        data: markAreas.map((a) => [
          { xAxis: a.from, itemStyle: { color: a.color } },
          { xAxis: a.to },
        ]),
      } : undefined,
      markLine: vLines.length ? {
        silent: true, symbol: "none",
        lineStyle: { color: "#8a94a6", type: "dashed", width: 1 },
        label: { color: "#8a94a6", fontSize: 10, position: "insideEndTop" },
        data: vLines.map((v) => [{ xAxis: v.date, label: { formatter: v.label } }, { xAxis: v.date }]),
      } : undefined,
    }];
    for (const l of extraLines) {
      series.push({
        name: l.name, type: "line", data: l.data, showSymbol: false,
        lineStyle: { width: 1, color: l.color, type: l.dashed ? "dashed" : "solid" },
      });
    }
    if (markPoints.length) {
      series.push({
        name: "转折点", type: "scatter", data: markPoints.map((p) => ({
          value: [p.date, p.price],
          itemStyle: { color: p.color },
          symbol: p.symbol, symbolSize: 9,
          label: { show: true, position: p.symbol.includes("triangleBar") ? "bottom" : "top", formatter: p.label, color: p.color, fontSize: 10 },
        })),
        z: 5,
      });
    }
    chart.setOption({
      backgroundColor: "transparent", animation: false,
      tooltip: { trigger: "axis" },
      grid: { left: 64, right: 24, top: 28, bottom: 44 },
      xAxis: { type: "category", data: dates },
      yAxis: { type: logY ? "log" : "value", scale: !logY },
      dataZoom: zoom ? [
        { type: "inside" },
        { type: "slider", height: 18, bottom: 8 },
      ] : undefined,
      series,
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => { window.removeEventListener("resize", onResize); ro.disconnect(); chart.dispose(); };
  }, [dates, data, height, logY, zoom, extraLines, markAreas, markPoints, vLines]);
  return <div ref={ref} className="chart" style={{ height }} />;
}

// ---------- 主页面 ----------

export default function Cycles() {
  const [subject, setSubject] = useState<"index" | "stock">("index");
  const [indexCode, setIndexCode] = useState("sh000001");
  const [stockInput, setStockInput] = useState("");
  const [stockCode, setStockCode] = useState("");
  const [swing, setSwing] = useState(30);
  const [data, setData] = useState<CyclesResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [juglar, setJuglar] = useState(true);

  const load = useCallback(() => {
    setBusy(true);
    setErr("");
    const q = new URLSearchParams();
    if (subject === "stock") {
      if (!stockCode.trim()) { setBusy(false); setData(null); return; }
      q.set("code", stockCode.trim());
      q.set("swing", String(swing));
    } else {
      q.set("index", indexCode);
      q.set("swing", String(swing));
    }
    api.get<CyclesResp>(`/api/cycles?${q}`).then((r) => { setData(r); }).catch((e) => { setErr(e.message); setData(null); })
      .finally(() => setBusy(false));
  }, [subject, indexCode, stockCode, swing]);

  useEffect(() => { load(); }, [load]);

  const switchSubject = (s: "index" | "stock") => {
    setSubject(s);
    setSwing(s === "stock" ? 50 : 30);
    if (s === "stock") setIndexCode(indexCode);
    else setStockCode("");
  };

  // ---------- 图表数据切片 ----------

  // 短周期：近 window 根 + MA20/60（前端自算，后端只给末值）
  const shortSlice = data ? data.dates.slice(-data.short.window) : [];
  const shortCloses = data ? data.closes.slice(-data.short.window) : [];
  const maOf = (w: number) => {
    if (!data) return [] as (number | null)[];
    const out: (number | null)[] = [];
    let s = 0;
    for (let i = 0; i < data.closes.length; i++) {
      s += data.closes[i];
      if (i >= w) s -= data.closes[i - w];
      out.push(i >= w - 1 ? s / w : null);
    }
    return out.slice(-data.short.window);
  };

  // 中周期：本轮起点到现在
  const medStart = data?.current?.start_idx ?? 0;
  const medDates = data ? data.dates.slice(medStart) : [];
  const medCloses = data ? data.closes.slice(medStart) : [];

  const stateBadge = (label: string, kind: "up" | "down" | "range") => (
    <span className={`badge ${kind === "up" ? "up" : kind === "down" ? "down" : ""}`}
      style={{ fontSize: 13 }}>{label}</span>
  );

  const pivotsToPoints = (pivots: Pivot[], prov?: Provisional | null) => {
    const pts = pivots.map((p) => ({
      date: p.date, price: p.price,
      color: p.type === "bottom" ? C_UP : C_DOWN,
      symbol: p.type === "bottom" ? "triangle" : "path://M0,0L8,0L4,7Z",
      label: p.type === "bottom" ? "底" : "顶",
    }));
    if (prov) pts.push({
      date: prov.date, price: prov.price,
      color: "#f7b500",
      symbol: "circle",
      label: `未确认${prov.type === "top" ? "顶" : "底"}${prov.retrace_pct.toFixed(0)}%`,
    });
    return pts;
  };

  const longAreas = data ? data.rounds.map((r) => ({
    from: r.from_date, to: r.to_date,
    color: r.kind === "bull" ? "rgba(239,83,80,0.10)" : "rgba(38,166,154,0.12)",
  })) : [];
  if (data?.current) longAreas.push({
    from: data.current.from_date, to: data.dates[data.dates.length - 1],
    color: data.current.kind === "bull" ? "rgba(239,83,80,0.16)" : "rgba(38,166,154,0.18)",
  });
  const juglarLines = data && juglar
    ? data.long.decades.map((y) => ({ date: `${y}-01-01`, label: `${y}·十年参考` }))
    : [];

  return (
    <>
      <div className="panel">
        <div className="row">
          <h3 style={{ margin: 0 }}>周期分析</h3>
          <span className="muted">
            短周期＝年内（{data?.short.swing ?? 10}% 波段）｜中周期＝本轮牛熊（{data?.meta.swing ?? 30}% 摆动确认）｜长周期＝全历史多轮牛熊
          </span>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className={`btn ${subject === "index" ? "primary" : ""}`} onClick={() => switchSubject("index")}>大盘指数</button>
          <button className={`btn ${subject === "stock" ? "primary" : ""}`} onClick={() => switchSubject("stock")}>个股</button>
          {subject === "index" ? (
            <select value={indexCode} onChange={(e) => setIndexCode(e.target.value)}>
              {INDEX_OPTIONS.map((o) => <option key={o.code} value={o.code}>{o.name}</option>)}
            </select>
          ) : (
            <>
              <input placeholder="6位代码，如 600519" value={stockInput} style={{ width: 150 }}
                onChange={(e) => setStockInput(e.target.value)} />
              <button className="btn" onClick={() => setStockCode(stockInput.trim())}>分析</button>
              {stockCode && <span className="badge">{stockCode}</span>}
            </>
          )}
          <span className="muted">牛熊摆动阈值</span>
          <select value={swing} onChange={(e) => setSwing(Number(e.target.value))}>
            {(subject === "stock" ? [30, 40, 50, 60] : [20, 25, 30]).map((v) => (
              <option key={v} value={v}>{v}%</option>
            ))}
          </select>
          <label className="row" style={{ gap: 4 }}>
            <input type="checkbox" checked={juglar} onChange={(e) => setJuglar(e.target.checked)} />
            <span className="muted">长周期十年参考网格（朱格拉）</span>
          </label>
          {busy && <span className="muted">加载中…</span>}
        </div>
        {data && (
          <div className="row" style={{ marginTop: 8 }}>
            <span className="badge">{data.meta.name}</span>
            <span className="muted">
              {data.meta.bars} 根日线 · {data.meta.first_date} ~ {data.meta.last_date} · {data.meta.price_note} · {data.meta.rounds_note}
              {data.meta.skipped_factor_rows > 0 && `（${data.meta.skipped_factor_rows} 行缺复权因子已跳过）`}
            </span>
          </div>
        )}
        {err && <div className="up" style={{ marginTop: 8 }}>{err}</div>}
      </div>

      {data && (
        <>
          {/* 三周期状态卡 */}
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))" }}>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>短周期 · 年内（近 {data.short.window} 个交易日{data.short.partial ? "（历史不足，全部展示）" : ""}）</h3>
              <div className="row" style={{ marginBottom: 6 }}>
                {stateBadge(data.short.state_label, data.short.state)}
                <span className="muted">区间位置 {data.short.pos_pct.toFixed(0)}%</span>
              </div>
              <ul className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {data.short.evidence.map((e, i) => <li key={i}>{e}</li>)}
              </ul>
            </div>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>中周期 · 本轮牛熊</h3>
              {data.current ? (
                <>
                  <div className="row" style={{ marginBottom: 6 }}>
                    {stateBadge(data.current.state, data.current.kind === "bull" ? "up" : "down")}
                    <span className="muted">
                      {data.current.kind_label} · 起点大{data.current.kind === "bull" ? "底" : "顶"} {data.current.from_date}（{data.current.from_price.toFixed(2)}）
                    </span>
                  </div>
                  <ul className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                    <li>已走 {data.current.months.toFixed(1)} 个月，{data.current.kind === "bull" ? "涨幅" : "涨跌"} {fp(data.current.chg_pct)}</li>
                    {data.current.kind === "bull"
                      ? <li>段内高点 {data.current.seg_hi.toFixed(2)}（{data.current.seg_hi_date}），当前回撤 {fp(-data.current.drawdown_pct!)}</li>
                      : <li>段内低点 {data.current.seg_lo.toFixed(2)}（{data.current.seg_lo_date}），当前反弹 {fp(data.current.rebound_pct!)}</li>}
                    {data.provisional && data.current.confirm_gap_pct != null && (
                      <li>
                        末端候选{data.provisional.type === "top" ? "顶" : "底"} {data.provisional.date}（{data.provisional.price.toFixed(2)}）：
                        再{data.provisional.type === "top" ? "跌" : "涨"}约 {data.current.confirm_gap_pct.toFixed(1)}% 即确认新转折
                      </li>
                    )}
                  </ul>
                </>
              ) : <p className="muted">历史不足以划分出牛熊转折（可尝试调低阈值）。</p>}
            </div>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>长周期 · 全历史位置</h3>
              <div className="row" style={{ marginBottom: 6 }}>
                {stateBadge(
                  data.long.current_kind === "bull" ? "处于牛市段" : data.long.current_kind === "bear" ? "处于熊市段" : "无已确认轮次",
                  data.long.current_kind === "bull" ? "up" : data.long.current_kind === "bear" ? "down" : "range")}
                <span className="muted">现价高于历史 {data.long.pctile.toFixed(0)}% 交易日的收盘</span>
              </div>
              <ul className="muted" style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
                {data.long.last_top && <li>距上一个确认大顶（{data.long.last_top.date}，{data.long.last_top.price.toFixed(2)}）：{fp(data.long.dist_last_top_pct)}</li>}
                {data.long.last_bottom && <li>距上一个确认大底（{data.long.last_bottom.date}，{data.long.last_bottom.price.toFixed(2)}）：{fp(data.long.dist_last_bottom_pct)}</li>}
                <li>
                  已确认 {data.long.rounds_count} 轮（牛 {data.long.bull_count} / 熊 {data.long.bear_count}）；
                  中位：牛 {fp(data.long.bull_med_chg_pct)} / {data.long.bull_med_months?.toFixed(1)} 个月，
                  熊 {fp(data.long.bear_med_chg_pct)} / {data.long.bear_med_months?.toFixed(1)} 个月
                </li>
                <li>康波（50~60 年）超出 A 股历史（{data.meta.first_date.slice(0, 4)} 起），无法用本数据验证，不作展示</li>
              </ul>
            </div>
          </div>

          {/* 短周期图 */}
          <div className="panel">
            <h3>短周期走势 · 近 12 个月（{data.short.swing}% 波段划分，MA20/MA60）</h3>
            <CycleLineChart
              dates={shortSlice} data={shortCloses} height={300}
              extraLines={[
                { name: "MA20", data: maOf(20), color: C_MA20 },
                { name: "MA60", data: maOf(60), color: C_MA60 },
              ]}
              markPoints={pivotsToPoints(data.short.pivots, data.short.provisional)}
              markAreas={data.short.pivots.flatMap((p, i, arr) => {
                if (i === 0) return [];
                const prev = arr[i - 1];
                return [{
                  from: prev.date, to: p.date,
                  color: prev.type === "bottom" ? "rgba(239,83,80,0.08)" : "rgba(38,166,154,0.10)",
                }];
              })}
            />
          </div>

          {/* 中周期图 */}
          {data.current && (
            <div className="panel">
              <h3>
                中周期走势 · 本轮{data.current.kind_label}（{data.current.from_date} 至今 · {data.current.months.toFixed(1)} 个月 · {fp(data.current.chg_pct)}）
              </h3>
              <CycleLineChart
                dates={medDates} data={medCloses} height={320}
                markPoints={pivotsToPoints(
                  data.pivots.filter((p) => p.date >= data.current!.from_date),
                  data.provisional && data.provisional.date >= data.current.from_date ? data.provisional : null,
                )}
                vLines={[{ date: data.current.from_date, label: `本轮起点 ${data.current.from_price.toFixed(2)}` }]}
              />
            </div>
          )}

          {/* 长周期图 */}
          <div className="panel">
            <h3>长周期走势 · 全历史（对数坐标 · 红=牛市段 绿=熊市段 · 深色=本轮 · 支持缩放拖动）</h3>
            <CycleLineChart
              dates={data.dates} data={data.closes} height={440}
              logY zoom
              markAreas={longAreas}
              markPoints={pivotsToPoints(data.pivots, data.provisional)}
              vLines={juglarLines}
            />
          </div>

          {/* 牛熊轮次表 */}
          <div className="panel">
            <h3>历史牛熊轮次（{data.meta.swing}% 摆动口径 · 按时间倒序）</h3>
            <div className="table-wrap" style={{ maxHeight: 360 }}>
              <table>
                <thead><tr>
                  <th className="l">轮次</th><th>类型</th><th>起点</th><th>起点价</th><th>终点</th><th>终点价</th>
                  <th>涨跌幅</th><th>时长(月)</th>
                </tr></thead>
                <tbody>
                  {[...data.rounds].reverse().map((r) => (
                    <tr key={r.label}>
                      <td className="l">{r.label}</td>
                      <td className={r.kind === "bull" ? "up" : "down"}>{r.kind_label}</td>
                      <td>{r.from_date}</td><td>{r.from_price.toFixed(2)}</td>
                      <td>{r.to_date}</td><td>{r.to_price.toFixed(2)}</td>
                      <td className={pctCls(r.chg_pct)}>{fp(r.chg_pct)}</td>
                      <td>{r.months.toFixed(1)}</td>
                    </tr>
                  ))}
                  {data.current && (
                    <tr>
                      <td className="l">{data.current.from_date.slice(0, 7)}→进行中</td>
                      <td className={data.current.kind === "bull" ? "up" : "down"}>{data.current.kind_label}</td>
                      <td>{data.current.from_date}</td><td>{data.current.from_price.toFixed(2)}</td>
                      <td>{data.meta.last_date}</td><td>{data.closes[data.closes.length - 1].toFixed(2)}</td>
                      <td className={pctCls(data.current.chg_pct)}>{fp(data.current.chg_pct)}</td>
                      <td>{data.current.months.toFixed(1)}</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          {/* 历史相位类比 */}
          {data.analogy && (
            <div className="panel">
              <h3>历史相位类比（当前相位 → 历史同相位的后续走势）</h3>
              <p className="muted" style={{ marginTop: 0 }}>
                当前相位：{data.analogy.phase.kind_label} 第 {data.analogy.phase.months.toFixed(1)} 个月（起点 {data.analogy.phase.from_date}，{fp(data.analogy.phase.chg_pct)}）。
                对齐规则：历史同类轮次中走出同样时长的时间点（±2 个月内），统计其后 3/6/12 个月的涨跌。
                <span className="up"> 历史分布是统计参考，不是预测。</span>
              </p>
              {data.analogy.note && <div className="muted" style={{ marginBottom: 8 }}>{data.analogy.note}</div>}
              <div className="row" style={{ marginBottom: 12 }}>
                {[3, 6, 12].map((k) => {
                  const s = data.analogy!.stats[`fwd${k}`];
                  return (
                    <div key={k} className="badge" style={{ padding: "8px 12px", textAlign: "left" }}>
                      <div>后 {k} 个月{s ? `（N=${s.n}）` : "（样本不足）"}</div>
                      {s ? (
                        <div style={{ marginTop: 4 }}>
                          中位 <span className={pctCls(s.p50)}>{fp(s.p50)}</span>｜
                          P25 {fp(s.p25)} / P75 {fp(s.p75)}｜
                          上涨概率 <span className={pctCls(s.win_rate - 50)}>{s.win_rate.toFixed(0)}%</span>
                        </div>
                      ) : <div className="muted">-</div>}
                    </div>
                  );
                })}
              </div>
              {data.analogy.cases.length > 0 && (
                <div className="table-wrap" style={{ maxHeight: 320 }}>
                  <table>
                    <thead><tr>
                      <th className="l">历史轮次</th><th>对齐时点</th><th>时点价</th><th>相位涨幅</th>
                      <th>该轮总涨幅</th><th>后3个月</th><th>后6个月</th><th>后12个月</th>
                    </tr></thead>
                    <tbody>
                      {data.analogy.cases.map((c) => (
                        <tr key={c.label}>
                          <td className="l">{c.label}</td>
                          <td>{c.at_date}</td><td>{c.at_price.toFixed(2)}</td>
                          <td className={pctCls(c.phase_chg_pct)}>{fp(c.phase_chg_pct)}</td>
                          <td className={pctCls(c.round_chg_pct)}>{fp(c.round_chg_pct)}</td>
                          <td className={pctCls(c.fwd3)}>{fp(c.fwd3)}</td>
                          <td className={pctCls(c.fwd6)}>{fp(c.fwd6)}</td>
                          <td className={pctCls(c.fwd12)}>{fp(c.fwd12)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {data.analogy.ended_before.length > 0 && (
                <p className="muted" style={{ marginBottom: 0, fontSize: 13 }}>
                  在当前相位之前就已结束的同类轮次（不计入统计，避免删失偏乐观）：
                  {data.analogy.ended_before.map((e) => `${e.label}（${e.months.toFixed(1)}个月，${fp(e.chg_pct)}）`).join("、")}
                </p>
              )}
            </div>
          )}

          {/* 月度季节性 */}
          <div className="panel">
            <h3>年内季节性 · 各自然月涨跌统计（{data.seasonality[0]?.n ? `${data.seasonality.find((s) => s.n)?.n ?? 0} 个年度样本` : ""}）</h3>
            <p className="muted" style={{ marginTop: 0 }}>
              月末收盘对上月末的涨跌幅，按自然月聚合；即「短周期=年内规律」的统计版（如 2 月春季躁动）。
              <span className="up"> 季节性是长期统计倾向，单年可以完全不遵守。</span>
            </p>
            <SeasonalityChart rows={data.seasonality} />
            <div className="table-wrap" style={{ maxHeight: 220, marginTop: 10 }}>
              <table>
                <thead><tr>
                  <th className="l">月份</th><th>均值</th><th>中位数</th><th>上涨概率</th><th>样本数</th>
                </tr></thead>
                <tbody>
                  {data.seasonality.map((s) => (
                    <tr key={s.month}>
                      <td className="l">{s.month} 月</td>
                      <td className={pctCls(s.mean_pct)}>{fp(s.mean_pct)}</td>
                      <td className={pctCls(s.median_pct)}>{fp(s.median_pct)}</td>
                      <td>{s.win_rate == null ? "-" : `${s.win_rate.toFixed(0)}%`}</td>
                      <td>{s.n}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function SeasonalityChart({ rows }: { rows: SeasonRow[] }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    chart.setOption({
      backgroundColor: "transparent", animation: false,
      tooltip: { trigger: "axis" },
      legend: { top: 0, textStyle: { color: "#8a94a6" } },
      grid: { left: 56, right: 56, top: 30, bottom: 28 },
      xAxis: { type: "category", data: rows.map((r) => `${r.month}月`) },
      yAxis: [
        { type: "value", name: "均值%" },
        { type: "value", name: "胜率%", max: 100, min: 0 },
      ],
      series: [
        {
          name: "月均涨跌幅", type: "bar",
          data: rows.map((r) => ({
            value: r.mean_pct,
            itemStyle: { color: (r.mean_pct ?? 0) > 0 ? C_UP : C_DOWN },
          })),
        },
        {
          name: "上涨概率", type: "line", yAxisIndex: 1,
          data: rows.map((r) => r.win_rate),
          showSymbol: true, lineStyle: { width: 1.5, color: C_LINE }, itemStyle: { color: C_LINE },
        },
      ],
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => { window.removeEventListener("resize", onResize); ro.disconnect(); chart.dispose(); };
  }, [rows]);
  return <div ref={ref} className="chart" style={{ height: 280 }} />;
}
