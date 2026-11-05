import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts";
import { api } from "../api";

// ---------- 与 backend/app/stockcycle.py 的返回结构一一对应 ----------

interface LevelRow {
  level1: string; n_sample: number;
  n_inv: number; n_cap: number; n_capacity: number; n_contract: number;
  inv_yoy_sum: number | null; inv_yoy_med: number | null;
  cap_yoy_sum: number | null; cap_yoy_med: number | null;
  capacity_yoy_sum: number | null; contract_yoy_sum: number | null;
  capex_intensity: number | null;
  quadrant: string | null; label: string; note: string;
}
interface Resp {
  ok: boolean; reason?: string; action?: string;
  periods: Record<string, string | null>;
  denominator?: { n_balance: number; n_cash: number };
  meta?: {
    min_cover: number; min_codes: number; flat_pp: number; n_levels: number; n_ready: number;
    caveats?: string[]; quadrant_counts?: Record<string, number>;
  };
  levels: LevelRow[];
}
interface SeriesRow {
  report_date: string;
  inv_yi: number | null; inv_yoy: number | null; inv_n: number;
  capex_yi: number | null; capex_yoy: number | null; capex_n: number;
  capacity_yi: number | null; capacity_yoy: number | null; capacity_n: number;
  contract_yi: number | null; contract_yoy: number | null; contract_n: number;
}
interface SeriesResp { level1: string; n_sample: number; series: SeriesRow[] }

const C_UP = "#ef5350", C_DOWN = "#26a69a", C_BAR = "#4f8cff";
const Q_COLOR: Record<string, string> = {
  "补库 + 扩产": C_UP,
  "库存堆积、投放收敛": "#f7b500",
  "去库 + 收缩": C_DOWN,
  "去库 + 扩产": C_BAR,
};

const fp = (v: number | null | undefined) => (v == null ? "-" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);
const pctCls = (v: number | null | undefined) => (v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "");

/** 同比读数：合计口径为主，等权中位用括号跟在后面（两个数不一致＝大小公司结构差）。 */
function pair(sum: number | null, med: number | null, n: number) {
  if (sum == null) return <span className="muted">数据不足（{n} 只）</span>;
  return (
    <span>
      <span className={pctCls(sum)}>{fp(sum)}</span>
      <span className="muted" title="等权口径：逐只同比的中位"> 中位 {fp(med)}</span>
    </span>
  );
}

// ---------- 四象限散点（点一下就在下面展开该行业的逐期序列） ----------

function QuadChart({ rows, onPick }: { rows: LevelRow[]; onPick: (l1: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const pts = useMemo(
    () => rows.filter((r) => r.inv_yoy_sum != null && r.cap_yoy_sum != null),
    [rows]);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    chart.setOption({
      backgroundColor: "transparent", animation: false,
      tooltip: {
        formatter: (p: any) => {
          const r = pts[p.dataIndex];
          return `${r.level1}<br/>存货同比 ${fp(r.inv_yoy_sum)}（${r.n_inv} 只）`
            + `<br/>资本开支同比 ${fp(r.cap_yoy_sum)}（${r.n_cap} 只）<br/>${r.label}`;
        },
      },
      grid: { left: 58, right: 24, top: 24, bottom: 46 },
      xAxis: {
        type: "value", name: "存货同比 %", nameLocation: "middle", nameGap: 26,
        axisLabel: { color: "#8a94a6", fontSize: 10 },
        splitLine: { lineStyle: { color: "rgba(138,148,166,0.12)" } },
      },
      yAxis: {
        type: "value", name: "资本开支同比 %",
        nameTextStyle: { color: "#8a94a6", fontSize: 10 },
        axisLabel: { color: "#8a94a6", fontSize: 10 },
        splitLine: { lineStyle: { color: "rgba(138,148,166,0.12)" } },
      },
      series: [{
        type: "scatter", symbolSize: 16, data: pts.map((r) => ({
          value: [r.inv_yoy_sum, r.cap_yoy_sum],
          itemStyle: { color: Q_COLOR[r.quadrant ?? ""] ?? "#8a94a6" },
        })),
        label: {
          show: true, position: "right", color: "#c8d0dc", fontSize: 10,
          formatter: (p: any) => pts[p.dataIndex]?.level1 ?? "",
        },
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: "rgba(200,208,220,0.35)", type: "dashed" },
          label: { show: false },
          data: [{ xAxis: 0 }, { yAxis: 0 }],
        },
      }],
    });
    chart.on("click", (p: any) => {
      const r = pts[p.dataIndex];
      if (r) onPick(r.level1);
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => {
      window.removeEventListener("resize", onResize); ro.disconnect(); chart.dispose();
    };
  }, [pts, onPick]);

  if (!pts.length) return <p className="muted">两个轴都有数的行业还不足一个，看图没意义。</p>;
  return <div ref={ref} className="chart" style={{ height: 380 }} />;
}

// ---------- 下钻：合计（柱）+ 同期同比（线） ----------

function DuoChart({ dates, bar, barName, line, lineName }: {
  dates: string[]; bar: (number | null)[]; barName: string;
  line: (number | null)[]; lineName: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    chart.setOption({
      backgroundColor: "transparent", animation: false,
      tooltip: { trigger: "axis" },
      legend: { textStyle: { color: "#c8d0dc", fontSize: 11 }, top: 0 },
      grid: { left: 56, right: 58, top: 30, bottom: 30 },
      xAxis: { type: "category", data: dates, axisLabel: { color: "#8a94a6", fontSize: 10 } },
      yAxis: [
        {
          type: "value", name: barName, scale: true,
          nameTextStyle: { color: "#8a94a6", fontSize: 10 },
          axisLabel: { color: "#8a94a6", fontSize: 10 },
          splitLine: { lineStyle: { color: "rgba(138,148,166,0.15)" } },
        },
        {
          type: "value", name: lineName,
          nameTextStyle: { color: "#8a94a6", fontSize: 10 },
          axisLabel: { color: "#8a94a6", fontSize: 10, formatter: "{value}%" },
          splitLine: { show: false },
        },
      ],
      series: [
        { name: barName, type: "bar", data: bar, itemStyle: { color: C_BAR }, barMaxWidth: 26 },
        {
          name: lineName, type: "line", yAxisIndex: 1, data: line, connectNulls: false,
          lineStyle: { width: 1.6, color: C_UP }, itemStyle: { color: C_UP }, symbolSize: 5,
        },
      ],
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => {
      window.removeEventListener("resize", onResize); ro.disconnect(); chart.dispose();
    };
  }, [dates, bar, barName, line, lineName]);
  return <div ref={ref} className="chart" style={{ height: 260 }} />;
}

type SortKey = "inv" | "capex" | "intensity";

export default function IndustryCycle() {
  const [data, setData] = useState<Resp | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [showCaveat, setShowCaveat] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>("inv");
  const [level, setLevel] = useState<string | null>(null);
  const [nPeriods, setNPeriods] = useState(16);
  const [ser, setSer] = useState<SeriesResp | null>(null);
  const [serErr, setSerErr] = useState("");
  const [serBusy, setSerBusy] = useState(false);

  const load = useCallback(() => {
    setBusy(true);
    setErr("");
    api.get<Resp>("/api/cycle/industry").then(setData)
      .catch((e) => { setErr(e.message); setData(null); })
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!level) { setSer(null); return; }
    setSerBusy(true);
    setSerErr("");
    api.get<SeriesResp>(`/api/cycle/industry/series?level1=${encodeURIComponent(level)}&periods=${nPeriods}`)
      .then(setSer)
      .catch((e) => { setSerErr(e.message); setSer(null); })
      .finally(() => setSerBusy(false));
  }, [level, nPeriods]);

  const rows = useMemo(() => {
    const src = data?.levels ?? [];
    const key = (x: LevelRow) =>
      sortKey === "inv" ? x.inv_yoy_sum : sortKey === "capex" ? x.cap_yoy_sum : x.capex_intensity;
    return [...src].sort((a, b) => {
      const va = key(a), vb = key(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return vb - va;
    });
  }, [data, sortKey]);

  const dates = useMemo(() => (ser?.series ?? []).map((x) => x.report_date).reverse(), [ser]);
  const rev = useMemo(() => [...(ser?.series ?? [])].reverse(), [ser]);

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3 style={{ margin: 0 }}>行业库存与资本周期（财报侧）</h3>
          <div className="row">
            <button className="btn" onClick={() => setShowCaveat((v) => !v)}>
              {showCaveat ? "收起口径与偏差" : "口径与偏差"}
            </button>
            <button className="btn" onClick={load} disabled={busy}>
              {busy ? "计算中…" : "重算"}
            </button>
          </div>
        </div>
        <p className="muted" style={{ margin: "6px 0 0", fontSize: 13 }}>
          横轴＝存货比去年同期、纵轴＝资本开支比去年同期，把有财报数据的申万一级行业摆在四格里
          （银行、非银金融报表口径不同，不抓）。
          只回答「这个行业现在在补库还是去库、在扩产还是在收缩」，不给买卖排序。
        </p>
        {data?.ok && (
          <div className="row muted" style={{ gap: 16, marginTop: 8, fontSize: 13, flexWrap: "wrap" }}>
            <span>负债表期 {data.periods.balance}（对 {data.periods.balance_prev}）</span>
            <span>现金流期 {data.periods.cash}（对 {data.periods.cash_prev}）</span>
            <span>样本名单 {data.denominator?.n_balance} 只（负债表）/ {data.denominator?.n_cash} 只（现金流）</span>
            <span>有数 {data.meta?.n_levels} 个行业，其中 {data.meta?.n_ready} 个进象限</span>
          </div>
        )}
        {showCaveat && data?.meta?.caveats && (
          <ul className="muted" style={{ margin: "8px 0 0", paddingLeft: 18, fontSize: 13 }}>
            {data.meta.caveats.map((c, i) => <li key={i}>{c}</li>)}
          </ul>
        )}
        {err && <div className="up" style={{ marginTop: 8 }}>{err}</div>}
        {data && !data.ok && (
          <div className="panel" style={{ marginTop: 10, marginBottom: 0 }}>
            <h3>还没有财报数据</h3>
            <p className="muted" style={{ fontSize: 13 }}>{data.reason}</p>
            <p style={{ margin: "6px 0 0", fontSize: 13 }}>{data.action}</p>
          </div>
        )}
      </div>

      {data?.ok && (
        <>
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))" }}>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3>四象限定位</h3>
              <div className="row muted" style={{ gap: 12, fontSize: 12, marginBottom: 4, flexWrap: "wrap" }}>
                {Object.entries(data.meta?.quadrant_counts ?? {}).map(([k, v]) => (
                  <span key={k} style={{ color: Q_COLOR[k] ?? "#8a94a6" }}>{k} {v} 个</span>
                ))}
                <span className="muted">（点行业名或格子可下钻）</span>
              </div>
              <QuadChart rows={rows} onPick={setLevel} />
            </div>

            <div className="panel" style={{ marginBottom: 0 }}>
              <div className="row" style={{ justifyContent: "space-between", marginBottom: 6 }}>
                <h3 style={{ margin: 0 }}>行业明细</h3>
                <label className="row muted" style={{ fontSize: 12, gap: 6 }}>
                  排序
                  <select value={sortKey} onChange={(e) => setSortKey(e.target.value as SortKey)}>
                    <option value="inv">按存货同比</option>
                    <option value="capex">按资本开支同比</option>
                    <option value="intensity">按 capex/经营现金流</option>
                  </select>
                </label>
              </div>
              <div className="table-wrap" style={{ maxHeight: 380, overflow: "auto" }}>
                <table>
                  <thead>
                    <tr>
                      <th>行业</th><th>名单/配对</th><th>存货同比</th><th>资本开支同比</th>
                      <th>在建+固投</th><th>合同负债</th><th>capex/现金流</th><th>象限</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((x) => (
                      <tr key={x.level1} onClick={() => setLevel(x.level1)} style={{ cursor: "pointer" }}>
                        <td>{x.level1}</td>
                        <td className="muted">{x.n_sample} / {x.n_inv}·{x.n_cap}</td>
                        <td title={x.note}>{pair(x.inv_yoy_sum, x.inv_yoy_med, x.n_inv)}</td>
                        <td>{pair(x.cap_yoy_sum, x.cap_yoy_med, x.n_cap)}</td>
                        <td className={pctCls(x.capacity_yoy_sum)}>{fp(x.capacity_yoy_sum)}</td>
                        <td className={pctCls(x.contract_yoy_sum)}>{fp(x.contract_yoy_sum)}</td>
                        <td>{x.capex_intensity == null ? "-" : `${x.capex_intensity.toFixed(1)}%`}</td>
                        <td style={{ color: Q_COLOR[x.quadrant ?? ""] }}>{x.label}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
                「名单/配对」＝该行业进样本的只数 / 两期都有数的只数（存货·资本开支）。
                合计同比受大公司主导，中位是等权口径，两者不一致是结构差，不是算错。
              </p>
            </div>
          </div>

          <div className="panel">
            <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
              <h3 style={{ margin: 0 }}>{level ? `${level} · 逐期序列` : "逐期序列"}</h3>
              {level && (
                <label className="row muted" style={{ fontSize: 12, gap: 6 }}>
                  期数
                  <select value={nPeriods} onChange={(e) => setNPeriods(Number(e.target.value))}>
                    {[8, 12, 16, 20, 24].map((n) => <option key={n} value={n}>{n} 期</option>)}
                  </select>
                </label>
              )}
            </div>
            {!level && <p className="muted" style={{ fontSize: 13 }}>在上面的散点或明细里点一个行业。</p>}
            {level && serBusy && <p className="muted">加载中… {serErr}</p>}
            {level && !serBusy && ser && (
              <>
                <div className="row muted" style={{ gap: 12, fontSize: 12, margin: "6px 0" }}>
                  <span>样本 {ser.n_sample} 只</span>
                  <span>每期的同比按「这期和去年同期都有数」的那批票重算，配对数随行显示</span>
                  <span>某期该口径披露的票不足样本六成时，那期只给配对数、不给合计与同比</span>
                </div>
                <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))" }}>
                  <div>
                    <h3 style={{ fontSize: 13 }}>库存</h3>
                    <DuoChart dates={dates} bar={rev.map((x) => x.inv_yi)} barName="存货合计（亿元）"
                      line={rev.map((x) => x.inv_yoy)} lineName="存货同比" />
                  </div>
                  <div>
                    <h3 style={{ fontSize: 13 }}>资本开支</h3>
                    <DuoChart dates={dates} bar={rev.map((x) => x.capex_yi)} barName="资本开支合计（亿元）"
                      line={rev.map((x) => x.capex_yoy)} lineName="资本开支同比" />
                  </div>
                </div>
                <div className="table-wrap" style={{ marginTop: 8, maxHeight: 260, overflow: "auto" }}>
                  <table>
                    <thead>
                      <tr>
                        <th>报告期</th><th>存货（亿）</th><th>同比</th><th>配对</th>
                        <th>资本开支（亿）</th><th>同比</th><th>配对</th>
                        <th>在建+固投（亿）</th><th>同比</th><th>合同负债（亿）</th><th>同比</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...ser.series].reverse().map((x) => (
                        <tr key={x.report_date}>
                          <td>{x.report_date}</td>
                          <td>{x.inv_yi == null ? "-" : x.inv_yi.toFixed(0)}</td>
                          <td className={pctCls(x.inv_yoy)}>{fp(x.inv_yoy)}</td>
                          <td className="muted">{x.inv_n}</td>
                          <td>{x.capex_yi == null ? "-" : x.capex_yi.toFixed(0)}</td>
                          <td className={pctCls(x.capex_yoy)}>{fp(x.capex_yoy)}</td>
                          <td className="muted">{x.capex_n}</td>
                          <td>{x.capacity_yi == null ? "-" : x.capacity_yi.toFixed(0)}</td>
                          <td className={pctCls(x.capacity_yoy)}>{fp(x.capacity_yoy)}</td>
                          <td>{x.contract_yi == null ? "-" : x.contract_yi.toFixed(0)}</td>
                          <td className={pctCls(x.contract_yoy)}>{fp(x.contract_yoy)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
            {level && !serBusy && !ser && serErr && <div className="up" style={{ marginTop: 6 }}>{serErr}</div>}
          </div>
        </>
      )}
    </>
  );
}
