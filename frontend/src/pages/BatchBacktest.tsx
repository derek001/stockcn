import { useEffect, useMemo, useRef, useState } from "react";
import KLineChart, { Bar } from "../KLineChart";
import { api, fmt } from "../api";
import { minusYears, ymd } from "../util";
import type { DrillDown } from "./Backtest";

interface StrategyMeta {
  id: string; name: string; enabled: boolean;
  params_schema: { key: string; label: string; type: string; default: number }[];
}
interface YearRow { year: string; count: number; median: number; mean: number; win_rate: number }
interface Bin { lo: number | null; hi: number | null; count: number }
interface Dist {
  count?: number; min?: number; max?: number; mean?: number;
  quantiles: Record<string, number>; bins: Bin[];
}
interface Summary {
  win_count: number; win_rate: number | null; beat_bh_count: number;
  pnl_median: number | null; pnl_mean: number | null;
  pnl_p25: number | null; pnl_p75: number | null;
  pnl_best: number | null; pnl_worst: number | null;
  excess_median: number | null; excess_p25: number | null; excess_p75: number | null;
  buy_count_mean: number | null; max_dd_median: number | null;
  fees_total: number; missed_total: number; missed_stocks: number;
  years: YearRow[]; dist: Record<string, Dist>;
  curve: { dates: string[]; eq: (number | null)[] };
  elapsed_load: number; elapsed_walk: number;
}
interface BatchLite {
  id: string; run_at: string; strategy_id: string; strategy_name: string;
  start: string; end: string; capital: number; stock_count: number;
  skipped: number; elapsed_sec: number;
}
interface BatchHead extends BatchLite {
  fee_rate: number; params: Record<string, number>; summary: Summary;
}
interface ItemRow {
  code: string; name: string; industry: string; bars: number;
  buy_count: number; order_count: number; missed: number;
  pnl_pct: number; bh_pct: number; excess_pct: number;
  max_dd_pct: number | null; fees: number; years: Record<string, number>;
}
interface Status {
  status: string; progress: number; total: number; message: string;
  batch_id: string; started_at: string;
}
interface ResultRes { batch: BatchHead; total: number; sort: string; desc: boolean; rows: ItemRow[] }
interface Curve { dates: string[]; eq: (number | null)[] }
interface CurveRes extends Curve {
  batch_id: string; top_n: number; label: string; biased: boolean;
}

const PAGE = 50;
const COLS: { key: string; label: string; num: boolean }[] = [
  { key: "code", label: "代码", num: false },
  { key: "name", label: "名称", num: false },
  { key: "industry", label: "行业", num: false },
  { key: "bars", label: "K线根数", num: true },
  { key: "buy_count", label: "买入次数", num: true },
  { key: "order_count", label: "委托数", num: true },
  { key: "missed", label: "买不起一手", num: true },
  { key: "pnl_pct", label: "策略收益%", num: true },
  { key: "bh_pct", label: "买入持有%", num: true },
  { key: "excess_pct", label: "超额%", num: true },
  { key: "max_dd_pct", label: "最大回撤%", num: true },
  { key: "fees", label: "手续费(元)", num: true },
];
const Q_LABELS: [string, string][] = [
  ["p05", "最差 5%"], ["p10", "P10"], ["p25", "P25"], ["p50", "中位 P50"],
  ["p75", "P75"], ["p90", "P90"], ["p95", "最好 5%"],
];
const DIST_NAME: Record<string, string> = {
  pnl: "策略区间收益%",
  excess: "超额收益%（策略 − 买入持有）",
};

function binLabel(b: Bin): string {
  if (b.lo === null) return `< ${b.hi}%`;
  if (b.hi === null) return `> ${b.lo}%`;
  return `${b.lo} ~ ${b.hi}%`;
}

function curveBars(c?: Curve | null): Bar[] {
  if (!c?.dates) return [];
  return c.dates.map((d, i) => ({ date: d, close: c.eq[i] }));
}

export default function BatchBacktest({ onBacktest }: { onBacktest: (d: DrillDown) => void }) {
  const [mode, setMode] = useState<"years" | "dates">("years");
  const [years, setYears] = useState(10);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [capital, setCapital] = useState(1000000);
  const [feeRate, setFeeRate] = useState(0.0003);
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [sid, setSid] = useState("");
  const [params, setParams] = useState<Record<string, number>>({});
  const [st, setSt] = useState<Status | null>(null);
  const [batches, setBatches] = useState<BatchLite[]>([]);
  const [bid, setBid] = useState("");
  const [res, setRes] = useState<ResultRes | null>(null);
  const [sort, setSort] = useState("excess_pct");
  const [desc, setDesc] = useState(true);
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const [distKey, setDistKey] = useState("pnl");
  const [topN, setTopN] = useState(20);
  const [topCurve, setTopCurve] = useState<CurveRes | null>(null);
  const [err, setErr] = useState("");
  const prevStatus = useRef("");

  const effStart = mode === "years" ? minusYears(ymd(new Date()), years) : start;
  const effEnd = mode === "years" ? ymd(new Date()) : (end || ymd(new Date()));
  const rangeMsg = mode === "dates" && !start ? "还没选开始日期"
    : effEnd <= effStart ? `结束日期 ${effEnd} 不晚于开始日期 ${effStart}` : "";

  const chosen = useMemo(() => strategies.find((s) => s.id === sid), [strategies, sid]);
  useEffect(() => {
    api.get<StrategyMeta[]>("/api/strategies").then((ls) => {
      const on = ls.filter((s) => s.enabled);
      setStrategies(on);
      if (on.length) {
        setSid(on[0].id);
        setParams(Object.fromEntries(on[0].params_schema.map((p) => [p.key, p.default])));
      }
    }).catch((e) => setErr((e as Error).message));
  }, []);
  useEffect(() => {
    if (chosen) setParams(Object.fromEntries(chosen.params_schema.map((p) => [p.key, p.default])));
  }, [sid]); // eslint-disable-line

  const loadList = () =>
    api.get<{ batches: BatchLite[] }>("/api/backtest/batch/list")
      .then((r) => setBatches(r.batches));

  const loadResult = (id: string, s: string, d: boolean, o: number, kw: string) =>
    api.get<ResultRes>(
      `/api/backtest/batch/result?batch_id=${id}&sort=${s}&desc=${d}` +
      `&limit=${PAGE}&offset=${o}&q=${encodeURIComponent(kw)}`)
      .then((r) => { setRes(r); setErr(""); })
      .catch((e) => setErr((e as Error).message));

  const loadCurve = (id: string, n: number) =>
    api.get<CurveRes>(`/api/backtest/batch/curve?batch_id=${id}&top_n=${n}`)
      .then((c) => setTopCurve(c))
      .catch((e) => setErr((e as Error).message));

  const openBatch = (id: string) => {
    if (!id) { setBid(""); setRes(null); setTopCurve(null); return; }
    setBid(id); setSort("excess_pct"); setDesc(true); setOffset(0); setQ("");
    setTopCurve(null);
    loadResult(id, "excess_pct", true, 0, "");
    loadCurve(id, topN);
  };

  // 只有跑批进行中才轮询进度：启动跑批后 bump 一次把轮询链接上，跑完自动停
  const [watching, setWatching] = useState(0);
  useEffect(() => {
    let stop = false;
    let timer: number | undefined;
    const tick = () => {
      api.get<Status>("/api/backtest/batch/status").then((s) => {
        if (stop) return;
        setSt(s);
        const was = prevStatus.current;
        prevStatus.current = s.status;
        if (s.status === "running") {
          timer = window.setTimeout(tick, 2000);
        } else if (was === "running" && s.batch_id) {
          loadList().then(() => openBatch(s.batch_id));
        }
      }).catch(() => { /* 服务没起来，10 秒后再看一次 */
        if (!stop) timer = window.setTimeout(tick, 10000);
      });
    };
    tick();
    return () => { stop = true; if (timer) window.clearTimeout(timer); };
  }, [watching]); // eslint-disable-line

  useEffect(() => { loadList(); }, []); // eslint-disable-line

  const running = st?.status === "running";
  const pct = st && st.total ? Math.round((st.progress / st.total) * 100) : running ? 5 : 0;

  const run = async () => {
    setErr("");
    try {
      const s = await api.post<Status>("/api/backtest/batch/run", {
        strategy_id: sid, capital, fee_rate: feeRate, start: effStart, end: effEnd, params,
      });
      setSt(s); prevStatus.current = "running";
      setWatching((w) => w + 1);   // 重开轮询链接上进度
    } catch (e) { setErr((e as Error).message); }
  };

  const head = res?.batch;
  const sum = head?.summary;
  const dist = sum?.dist?.[distKey];
  const maxBin = dist?.bins.reduce((a, b) => Math.max(a, b.count), 0) || 0;
  const applyQ = () => { setOffset(0); if (bid) loadResult(bid, sort, desc, 0, q); };
  const turn = (key: string) => {
    const d = sort === key ? !desc : true;
    setSort(key); setDesc(d); setOffset(0);
    if (bid) loadResult(bid, key, d, 0, q);
  };

  return (
    <>
      <div className="panel">
        <h3>全市场回测设置</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          不用输股票代码：选一个策略，把本地库里所有有日线的股票各跑一遍（每只单独给同样的初始资金）。
          出来的是一张成绩单——多少只赚钱、分布长什么样、跑赢买入持有的有几只，再按股票排名，
          点某一行会连着这一轮的策略、参数、区间一起跳到【回测】页，自动把这只跑出来（K线、买卖点、逐笔成交）。
        </p>
        <div className="row">
          <div className="field">回测区间
            <span className="row" style={{ gap: 6 }}>
              <input type="radio" name="bb-range" id="bb-years"
                checked={mode === "years"} onChange={() => setMode("years")} />
              <label htmlFor="bb-years">最近</label>
              <select value={years} disabled={mode !== "years"}
                onChange={(e) => setYears(Number(e.target.value))}>
                {[1, 3, 5, 10].map((y) => <option key={y} value={y}>{y} 年</option>)}
              </select>
              <input type="radio" name="bb-range" id="bb-dates"
                checked={mode === "dates"} onChange={() => setMode("dates")} />
              <label htmlFor="bb-dates">指定日期</label>
            </span>
          </div>
          {mode === "dates" && <>
            <label className="field">开始日期
              <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
            </label>
            <label className="field">结束日期(不填=今天)
              <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
            </label>
          </>}
          <label className="field">每只初始资金(元)
            <input type="number" style={{ width: 140 }} value={capital}
              onChange={(e) => setCapital(Number(e.target.value))} />
          </label>
          <label className="field">单笔手续费率
            <input type="number" step="0.0001" style={{ width: 110 }} value={feeRate}
              onChange={(e) => setFeeRate(Number(e.target.value))} />
          </label>
          <label className="field">交易策略
            <select value={sid} onChange={(e) => setSid(e.target.value)}>
              {strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
          {(chosen?.params_schema ?? []).map((p) => (
            <label className="field" key={p.key}>{p.label}
              <input type="number" style={{ width: 90 }} value={params[p.key] ?? p.default}
                onChange={(e) => setParams({ ...params, [p.key]: Number(e.target.value) })} />
            </label>
          ))}
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn primary" onClick={run}
            disabled={running || !sid || !!rangeMsg}>
            {running ? "全市场跑批中…" : "开始全市场回测"}
          </button>
          {running && <>
            <div className="progress"><div style={{ width: `${pct}%` }} /></div>
            <span>{pct}%</span>
          </>}
          <span className={running ? "up" : "muted"}>
            {running
              ? `${st?.message ?? ""}｜跑批期间整台服务都在忙，其它页面点了会转圈等待，等它跑完再操作。`
              : rangeMsg
                ? `先补全日期才能跑批：${rangeMsg}。`
                : `本次区间 ${effStart} ~ ${effEnd}（${mode === "years" ? `最近 ${years} 年` : "指定日期"}），`
                  + `全市场约 5500 只、要跑 1~2 分钟。`}
          </span>
        </div>
        {st && !running && st.status !== "idle" && (
          <div className={st.status === "error" ? "up" : "muted"} style={{ marginTop: 6 }}>
            {st.status === "error" ? st.message
              : `上一次：${st.message}（${st.started_at?.replace("T", " ").slice(5, 16)} 开始）`}
          </div>
        )}
        {err && <div className="up" style={{ marginTop: 6 }}>{err}</div>}
      </div>

      <div className="panel">
        <div className="row">
          <h3 style={{ margin: 0 }}>历史批次</h3>
          <select value={bid} onChange={(e) => openBatch(e.target.value)}>
            <option value="">选一个已留痕的批次…</option>
            {batches.map((b) => (
              <option key={b.id} value={b.id}>
                {b.run_at.replace("T", " ").slice(5, 16)}｜{b.strategy_name}｜
                {b.start}~{b.end}｜{b.stock_count} 只
              </option>
            ))}
          </select>
          <span style={{ flex: 1 }} />
          <span className="muted">{batches.length} 个批次留痕</span>
        </div>
        {head && (
          <div className="muted" style={{ marginTop: 6, fontSize: 12 }}>
            批次 {head.id}｜策略参数 {JSON.stringify(head.params)}｜
            每只 {fmt.n(head.capital)} 元、手续费 {fmt.n(head.fee_rate * 100, 3)}%｜
            耗时 {head.elapsed_sec}s（取数 {sum?.elapsed_load}s + 记账 {sum?.elapsed_walk}s）｜
            参与 {head.stock_count} 只，K线不足 30 根跳过 {head.skipped} 只
          </div>
        )}
      </div>

      {head && sum && (
        <>
          <div className="row" style={{ marginBottom: 16, flexWrap: "wrap", rowGap: 8 }}>
            {metric("正收益", `${sum.win_count} 只`,
              (sum.win_rate ?? 0) >= 50 ? "up" : "down",
              `扣完手续费还赚钱的股票数，占参与 ${head.stock_count} 只的 ${fmt.n(sum.win_rate, 1)}%`)}
            {metric("收益中位数", `${fmt.n(sum.pnl_median)}%`,
              (sum.pnl_median ?? 0) >= 0 ? "up" : "down",
              `随机抓一只的期望水平：一半股票比它好。较差的 1/4 低于 ${fmt.n(sum.pnl_p25)}%，`
              + `较好的 1/4 高于 ${fmt.n(sum.pnl_p75)}%`)}
            {metric("平均收益", `${fmt.n(sum.pnl_mean)}%`, "",
              "被少数大赚的股票拉高，所以看典型结果请看中位数，不要看这里")}
            {metric("跑赢买入持有", `${sum.beat_bh_count} 只`, "",
              `同一只股票「拿着不动」作对照，策略比它高的只数`
              + `（占 ${fmt.n((sum.beat_bh_count / head.stock_count) * 100, 1)}%）；`
              + `超额收益中位 ${fmt.n(sum.excess_median)}%，较差的 1/4 低于 ${fmt.n(sum.excess_p25)}%，`
              + `较好的 1/4 高于 ${fmt.n(sum.excess_p75)}%`)}
            {metric("最大回撤中位", `${fmt.n(sum.max_dd_median)}%`, "down",
              "净值从最高点跌到最低点的最大跌幅，一半股票比这个跌得深")}
            {metric("手续费合计", fmt.mv((sum.fees_total || 0) / 1e8), "",
              "所有股票所有成交的手续费之和（每只都单独配一笔初始资金）")}
            {metric("信号买不起一手", `${sum.missed_total} 次`, sum.missed_total ? "up" : "",
              sum.missed_stocks
                ? `${sum.missed_stocks} 只股票出现过：把每只初始资金调大再跑`
                : "没有错过")}
          </div>

          <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 16 }}>
            <div className="panel" style={{ margin: 0 }}>
              <h3>等权净值：全部 {head.stock_count} 只</h3>
              <div className="muted" style={{ fontSize: 12, marginTop: -6, marginBottom: 8 }}>
                每只起点 1.0（等于初始资金），逐日取所有股票净值的平均。
                这条线只说明「平均形状」：5500 只里既有腰斩的也有翻倍的，平均值看着平，
                真正的分布要看下面那张直方图和分位数。
              </div>
              <KLineChart bars={curveBars(sum.curve)} lineMode showMA={false} showVol={false}
                title="全部等权" height={300} />
            </div>
            <div className="panel" style={{ margin: 0 }}>
              <div className="row">
                <h3 style={{ margin: 0, flex: 1 }}>
                  等权净值：{topCurve?.label ?? `超额收益前 ${topN} 只`}
                </h3>
                <select value={topN} onChange={(e) => {
                  const n = Number(e.target.value);
                  setTopN(n);
                  if (bid) loadCurve(bid, n);
                }}>
                  {[20, 50, 100].map((n) => <option key={n} value={n}>前 {n} 只</option>)}
                </select>
              </div>
              <div className="up" style={{ fontSize: 12, marginTop: 4, marginBottom: 8 }}>
                注意：前 {topN} 只是拿这一批结果「事后按收益挑出来的」，实盘不可能提前知道是谁，
                所以这条线必然是这批里最好看的，只能当上限参考。
              </div>
              {topCurve
                ? <KLineChart bars={curveBars(topCurve)} lineMode showMA={false} showVol={false}
                    title={topCurve.label} height={300} />
                : <div className="muted">曲线加载中…（要重放这 {topN} 只的K线，几秒）</div>}
            </div>
          </div>

          {!!dist?.bins?.length && (
            <div className="panel">
              <div className="row">
                <h3 style={{ margin: 0, flex: 1 }}>
                  分布：{DIST_NAME[distKey] ?? distKey}（{fmt.n(dist.count, 0)} 只）
                </h3>
                <select value={distKey} onChange={(e) => setDistKey(e.target.value)}>
                  {Object.keys(sum.dist).map((k) => (
                    <option key={k} value={k}>{DIST_NAME[k] ?? k}</option>
                  ))}
                </select>
              </div>
              <div className="muted" style={{ fontSize: 12, marginTop: -4, marginBottom: 8 }}>
                一行一档，条长按最多的那一档拉满，右边是只数和占比。
                分位数读法：把 {fmt.n(dist.count, 0)} 只从最差排到最好，P10 就是第 10% 名的数
                （只有 10% 的股票比它更差），P50 即中位数。
              </div>
              {dist.bins.map((b, i) => (
                <div className="row" key={i} style={{ gap: 8, marginBottom: 3 }}>
                  <span className="muted" style={{ width: 110, textAlign: "right" }}>
                    {binLabel(b)}
                  </span>
                  <div className="progress">
                    <div style={{ width: `${maxBin ? (b.count / maxBin) * 100 : 0}%` }} />
                  </div>
                  <span style={{ width: 130 }}>
                    {b.count} 只 / {fmt.n((b.count / head.stock_count) * 100, 1)}%
                  </span>
                </div>
              ))}
              <div className="row" style={{ gap: 14, marginTop: 10, flexWrap: "wrap" }}>
                {Q_LABELS.map(([k, label]) => (
                  <span key={k} className="muted" style={{ fontSize: 12 }}>
                    {label}
                    <b style={{ marginLeft: 4, color: (dist.quantiles[k] ?? 0) >= 0 ? "#ef5350" : "#26a69a" }}>
                      {fmt.n(dist.quantiles[k])}%
                    </b>
                  </span>
                ))}
                <span className="muted" style={{ fontSize: 12 }}>
                  极值 {fmt.n(dist.min)}% ~ {fmt.n(dist.max)}%（这批里最差 / 最好那一只）
                </span>
              </div>
            </div>
          )}

          <div className="panel">
            <h3>按自然年分解</h3>
            <div className="muted" style={{ fontSize: 12, marginTop: -6, marginBottom: 8 }}>
              每年以「上一年最后一天的净值」为基准重算，所以各年连乘≈区间收益；
              那一年K线不足 2 根的股票（长停牌、那年才上市、年内已退市）不计入这一年，
              所以覆盖只数会少于参与总数，最后一年也只到结束日期为止。
            </div>
            <div className="table-wrap">
              <table>
                <thead><tr>
                  <th>年份</th><th>覆盖只数</th><th>收益中位%</th><th>收益均值%</th><th>正收益占比%</th>
                </tr></thead>
                <tbody>
                  {sum.years.map((y) => (
                    <tr key={y.year}>
                      <td>{y.year}</td>
                      <td>{fmt.n(y.count, 0)}</td>
                      <td className={y.median >= 0 ? "up" : "down"}>{fmt.n(y.median)}</td>
                      <td className={y.mean >= 0 ? "up" : "down"}>{fmt.n(y.mean)}</td>
                      <td>{fmt.n(y.win_rate, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="panel">
            <div className="row">
              <h3 style={{ margin: 0, flex: 1 }}>逐只排名（{res?.total ?? 0} 只）</h3>
              <input style={{ width: 150 }} placeholder="代码或名称含" value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") applyQ(); }} />
              <button className="btn" onClick={applyQ}>搜索</button>
              <button className="btn" disabled={offset <= 0}
                onClick={() => { const o = Math.max(0, offset - PAGE); setOffset(o); loadResult(bid, sort, desc, o, q); }}>
                上一页
              </button>
              <span className="muted">
                {res?.total ? `${offset + 1} ~ ${Math.min(offset + PAGE, res.total)} 条` : "0 条"}
              </span>
              <button className="btn" disabled={!res || offset + PAGE >= res.total}
                onClick={() => { const o = offset + PAGE; setOffset(o); loadResult(bid, sort, desc, o, q); }}>
                下一页
              </button>
            </div>
            <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
              点表头换排序（默认按超额收益从高到低），点一行去【回测】页：这一轮的策略、参数、区间、资金会原样带过去并自动跑一次。
            </div>
            <div className="table-wrap" style={{ marginTop: 8 }}>
              <table>
                <thead><tr>
                  {COLS.map((c) => (
                    <th key={c.key} className={c.num ? "" : "l"} onClick={() => turn(c.key)}
                      style={{ cursor: "pointer" }}>
                      {c.label}{sort === c.key ? (desc ? " ▼" : " ▲") : ""}
                    </th>
                  ))}
                  <th className="l">逐年收益%</th>
                </tr></thead>
                <tbody>
                  {(res?.rows ?? []).map((r) => (
                    <tr key={r.code} style={{ cursor: "pointer" }}
                      onClick={() => onBacktest({
                        code: r.code, strategy_id: head.strategy_id, params: head.params,
                        start: head.start, end: head.end,
                        capital: head.capital, fee_rate: head.fee_rate,
                      })}>
                      <td className="l">{r.code}</td>
                      <td className="l">{r.name}</td>
                      <td className="l">{r.industry || "-"}</td>
                      <td>{r.bars}</td>
                      <td>{r.buy_count}</td>
                      <td>{r.order_count}</td>
                      <td className={r.missed ? "up" : ""}>{r.missed}</td>
                      <td className={r.pnl_pct >= 0 ? "up" : "down"}>{fmt.n(r.pnl_pct)}</td>
                      <td className={r.bh_pct >= 0 ? "up" : "down"}>{fmt.n(r.bh_pct)}</td>
                      <td className={r.excess_pct >= 0 ? "up" : "down"}>{fmt.n(r.excess_pct)}</td>
                      <td>{fmt.n(r.max_dd_pct)}</td>
                      <td>{fmt.n(r.fees)}</td>
                      <td className="l muted">
                        {Object.entries(r.years).sort().map(([y, v]) =>
                          `${y.slice(2)}:${v}%`).join(" ") || "无整年交易"}
                      </td>
                    </tr>
                  ))}
                  {!res?.rows.length && (
                    <tr><td colSpan={COLS.length + 1} className="l muted">没有匹配的股票</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function metric(label: string, value: string, cls = "", tip = "") {
  return (
    <div className="panel" style={{ margin: 0, flex: 1, minWidth: 180 }} title={tip}>
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700 }} className={cls}>{value}</div>
    </div>
  );
}
