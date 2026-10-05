import { useEffect, useMemo, useState } from "react";
import KLineChart, { Bar } from "../KLineChart";
import { api, fmt } from "../api";

interface Trade {
  date: string; side: "buy" | "sell"; qty: number; price: number;
  amount: number; fee: number; pnl: number | null; pnl_pct: number | null; reason: string;
}
interface Report {
  id: string; code: string; stock_name: string; industry: string | null;
  strategy: { id: string; name: string; params: Record<string, number> };
  range: { start: string; end: string };
  capital: number; fee_rate: number;
  summary: {
    elapsed_sec: number; order_count: number; trade_count: number; missed_buy_signals: number;
    final_equity: number; pnl: number; pnl_pct: number; fees_total: number;
    index_name: string; index_pct: number | null; industry_pct: number | null;
  };
  stock_bars: Bar[]; markers: { date: string; type: "buy" | "sell"; price: number }[];
  index_bars: Bar[]; board_bars: Bar[]; trades: Trade[];
}
interface StrategyMeta {
  id: string; name: string; enabled: boolean;
  params_schema: { key: string; label: string; type: string; default: number }[];
}

export default function Backtest({ initialCode }: { initialCode?: string }) {
  const [code, setCode] = useState(initialCode ?? "600519");
  const [mode, setMode] = useState<"years" | "dates">("years");
  const [years, setYears] = useState(10);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [capital, setCapital] = useState(1000000);
  const [feeRate, setFeeRate] = useState(0.0003);
  const [strategies, setStrategies] = useState<StrategyMeta[]>([]);
  const [sid, setSid] = useState("");
  const [params, setParams] = useState<Record<string, number>>({});
  const [report, setReport] = useState<Report | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.get<StrategyMeta[]>("/api/strategies").then((ls) => {
      const on = ls.filter((s) => s.enabled);
      setStrategies(on);
      if (on.length && !on.some((s) => s.id === sid)) {
        setSid(on[0].id);
        setParams(Object.fromEntries(on[0].params_schema.map((p) => [p.key, p.default])));
      }
    });
  }, []); // eslint-disable-line

  const chosen = useMemo(() => strategies.find((s) => s.id === sid), [strategies, sid]);
  useEffect(() => {
    if (chosen) setParams(Object.fromEntries(chosen.params_schema.map((p) => [p.key, p.default])));
  }, [sid]); // eslint-disable-line

  const today = ymd(new Date());
  const effStart = mode === "years" ? minusYears(today, years) : start;
  const effEnd = mode === "years" ? today : (end || today);
  // 区间不成立的两类情况都拦在按钮上，别把「跑错段」留给用户猜
  const rangeMsg = mode === "dates" && !start ? "还没选开始日期"
    : effEnd <= effStart ? `结束日期 ${effEnd} 不晚于开始日期 ${effStart}` : "";

  const run = async () => {
    setBusy(true); setErr(""); setReport(null);
    try {
      const r = await api.post<Report>("/api/backtest", {
        code: code.trim(), strategy_id: sid, capital, fee_rate: feeRate,
        start: effStart, end: effEnd, params,
      });
      setReport(r);
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };

  return (
    <>
      <div className="panel">
        <h3>回测设置</h3>
        <div className="row">
          <label className="field">股票代码
            <input style={{ width: 110 }} value={code} onChange={(e) => setCode(e.target.value)} />
          </label>
          <div className="field">回测区间
            <span className="row" style={{ gap: 6 }}>
              <input type="radio" name="range-mode" id="rm-years"
                checked={mode === "years"} onChange={() => setMode("years")} />
              <label htmlFor="rm-years">最近</label>
              <select value={years} disabled={mode !== "years"}
                onChange={(e) => setYears(Number(e.target.value))}>
                {[1, 3, 5, 10].map((y) => <option key={y} value={y}>{y} 年</option>)}
              </select>
              <input type="radio" name="range-mode" id="rm-dates"
                checked={mode === "dates"} onChange={() => setMode("dates")} />
              <label htmlFor="rm-dates">指定日期</label>
            </span>
          </div>
          {mode === "dates" && <>
            <label className="field" htmlFor="bt-start">开始日期
              <input id="bt-start" type="date" value={start}
                onChange={(e) => setStart(e.target.value)} />
            </label>
            <label className="field" htmlFor="bt-end">结束日期(不填=今天)
              <input id="bt-end" type="date" value={end}
                onChange={(e) => setEnd(e.target.value)} />
            </label>
          </>}
          <label className="field">初始资金(元)
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
          <button className="btn primary" onClick={run} disabled={busy || !sid || !!rangeMsg}>
            {busy ? "回测运行中…" : "开始回测"}
          </button>
          <span className="muted">
            {rangeMsg
              ? `先补全日期才能回测：${rangeMsg}。`
              : `本次区间 ${effStart} ~ ${effEnd}（${mode === "years" ? `最近 ${years} 年` : "指定日期"}）。`}
            默认资金 100 万，手续费 0.03%。
          </span>
          {err && <span className="up">{err}</span>}
        </div>
      </div>

      {report && <ReportView r={report} />}
    </>
  );
}

function ymd(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function minusYears(iso: string, years: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const t = new Date(y, m - 1, d);
  t.setFullYear(t.getFullYear() - years);
  return ymd(t);
}

function ReportView({ r }: { r: Report }) {
  const s = r.summary;
  const metric = (label: string, value: string, cls = "") => (
    <div className="panel" style={{ margin: 0, flex: 1, minWidth: 170 }}>
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700 }} className={cls}>{value}</div>
    </div>
  );
  const pnlCls = s.pnl > 0 ? "up" : s.pnl < 0 ? "down" : "";
  return (
    <>
      <div className="row" style={{ marginBottom: 16 }}>
        {metric("回测耗时", `${s.elapsed_sec} 秒`)}
        {metric("买入笔数", `${s.trade_count} 笔`)}
        {metric("总委托数", `${s.order_count} 笔`)}
        {metric("信号买不起一手", `${s.missed_buy_signals} 次`)}
        {metric("盈亏金额(扣费)", `${fmt.n(s.pnl)} 元`, pnlCls)}
        {metric("盈亏比例(扣费)", `${fmt.n(s.pnl_pct)}%`, pnlCls)}
        {metric(`${s.index_name}涨跌`, `${fmt.n(s.index_pct)}%`,
          (s.index_pct ?? 0) > 0 ? "up" : "down")}
        {metric("行业指数涨跌", `${fmt.n(s.industry_pct)}%`,
          (s.industry_pct ?? 0) > 0 ? "up" : "down")}
      </div>

      {s.missed_buy_signals > 0 && (
        <div className="up" style={{ marginBottom: 12 }}>
          有 {s.missed_buy_signals} 个买入信号因「一手（100 股）买不起」被跳过：
          把初始资金调大，或换单价更低的股票再跑。
        </div>
      )}

      <div className="panel">
        <h3>{r.stock_name}({r.code}) K线 与买卖点 — {r.strategy.name}</h3>
        <div className="muted" style={{ fontSize: 12, marginTop: -6, marginBottom: 8 }}>
          回测区间 {r.range.start} ~ {r.range.end}｜实际K线 {r.stock_bars.length} 根
          （{r.stock_bars[0]?.date ?? "-"} ~ {r.stock_bars[r.stock_bars.length - 1]?.date ?? "-"}）
          ｜成交价与金额按不复权真实价，曲线和买卖点按复权价
        </div>
        <KLineChart bars={r.stock_bars} markers={r.markers} showMA showVol height={540} />
      </div>
      <div className="grid" style={{ gridTemplateColumns: "1fr 1fr", marginBottom: 16 }}>
        <div className="panel" style={{ margin: 0 }}>
          <h3>大盘指数：{s.index_name}</h3>
          <KLineChart bars={r.index_bars} lineMode showMA={false} showVol={false}
            title={s.index_name} height={300} />
        </div>
        <div className="panel" style={{ margin: 0 }}>
          <h3>行业指数：{r.industry ?? "-"}（成分股等权合成）</h3>
          <KLineChart bars={r.board_bars} lineMode showMA={false} showVol={false}
            title={r.industry ?? "行业"} height={300} />
        </div>
      </div>

      <div className="panel">
        <div className="row">
          <h3 style={{ margin: 0, flex: 1 }}>交易明细（{r.trades.length} 条委托）</h3>
          <a className="btn primary" href={`/api/backtest/${r.id}/excel`}
            style={{ textDecoration: "none" }}>导出 Excel</a>
        </div>
        <div className="table-wrap" style={{ marginTop: 10 }}>
          <table>
            <thead><tr>
              <th className="l">交易日期</th><th className="l">交易股票</th><th className="l">方向</th>
              <th>交易数量(股)</th><th>成交价</th><th>交易金额</th><th>手续费</th>
              <th>盈亏金额</th><th>盈亏比例</th><th className="l">触发原因</th>
            </tr></thead>
            <tbody>
              {r.trades.map((t, i) => (
                <tr key={i}>
                  <td className="l">{t.date}</td>
                  <td className="l">{r.code} {r.stock_name}</td>
                  <td className="l"><span className={t.side === "buy" ? "up" : "down"}>
                    {t.side === "buy" ? "买入" : "卖出"}</span></td>
                  <td>{fmt.n(t.qty, 2)}</td>
                  <td>{fmt.n(t.price, 3)}</td>
                  <td>{fmt.n(t.amount)}</td>
                  <td>{fmt.n(t.fee)}</td>
                  <td className={t.pnl == null ? "" : t.pnl >= 0 ? "up" : "down"}>
                    {t.pnl == null ? "-" : fmt.n(t.pnl)}</td>
                  <td dangerouslySetInnerHTML={{
                    __html: t.pnl_pct == null ? "-" : fmt.pct(t.pnl_pct) }} />
                  <td className="l muted">{t.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
