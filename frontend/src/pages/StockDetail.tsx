import { useEffect, useState } from "react";
import KLineChart, { Bar } from "../KLineChart";
import { api, fmt } from "../api";

interface Detail {
  info: {
    code: string; name: string; market: string; industry: string; list_date: string;
    price: number; pct_chg: number; total_mv: number; float_mv: number;
    pe_ttm: number; pe_dynamic: number; pb: number;
  } | null;
  financials: {
    report_date: string; eps: number; revenue: number; revenue_yoy: number;
    net_profit: number; net_profit_yoy: number; roe_weighted: number; gross_margin: number;
  }[];
  bars: Bar[];
}

export default function StockDetail({ code, onBack, onBacktest }:
  { code: string; onBack: () => void; onBacktest: () => void }) {
  const [d, setD] = useState<Detail | null>(null);
  const [err, setErr] = useState("");
  const [range, setRange] = useState("250");

  useEffect(() => {
    setD(null);
    api.get<Detail>(`/api/stocks/${code}/kline?indicators=ma,macd,boll`)
      .then(setD).catch((e) => setErr(e.message));
  }, [code]);

  if (err) return <div className="panel up">{err}</div>;
  if (!d) return <div className="panel">加载 {code} …</div>;

  const barsAll = d.bars;
  const bars = range === "all" ? barsAll : barsAll.slice(-Number(range));
  const info = d.info;

  return (
    <>
      <div className="panel row">
        <button className="btn" onClick={onBack}>← 返回</button>
        <h3 style={{ margin: 0 }}>{info?.name} ({info?.code})</h3>
        <span className="badge">{info?.market}</span>
        <span className="badge">{info?.industry ?? "未知行业"}</span>
        <span style={{ flex: 1 }} />
        <button className="btn primary" onClick={onBacktest}>用此股票回测</button>
      </div>
      <div className="panel">
        <div className="row" style={{ fontSize: 15 }}>
          <span>最新价 <b>{fmt.n(info?.price)}</b></span>
          <span dangerouslySetInnerHTML={{ __html: fmt.pct(info?.pct_chg) }} />
          <span className="muted">总市值 {info?.total_mv ? fmt.mv(info.total_mv / 1e8) : "-"}</span>
          <span className="muted">流通市值 {info?.float_mv ? fmt.mv(info.float_mv / 1e8) : "-"}</span>
          <span className="muted">PE(TTM) {fmt.n(info?.pe_ttm)}</span>
          <span className="muted">PB {fmt.n(info?.pb)}</span>
          <span className="muted">上市日期 {info?.list_date ?? "-"}</span>
        </div>
      </div>
      <div className="panel">
        <div className="row" style={{ marginBottom: 8 }}>
          <h3 style={{ margin: 0, flex: 1 }}>日K线（后复权）</h3>
          <select value={range} onChange={(e) => setRange(e.target.value)}>
            <option value="120">近半年</option>
            <option value="250">近一年</option>
            <option value="750">近三年</option>
            <option value="all">全部</option>
          </select>
        </div>
        <KLineChart bars={bars} showMA showBOLL showMACD showVol height={560} />
      </div>
      <div className="panel">
        <h3>季度财务数据</h3>
        <div className="table-wrap" style={{ maxHeight: 300 }}>
          <table>
            <thead><tr>
              <th className="l">报告期</th><th>EPS(元)</th><th>营收(亿)</th><th>营收同比</th>
              <th>净利润(亿)</th><th>净利同比</th><th>ROE加权</th><th>毛利率</th>
            </tr></thead>
            <tbody>
              {d.financials.map((f) => (
                <tr key={f.report_date}>
                  <td className="l">{f.report_date}</td>
                  <td>{fmt.n(f.eps)}</td>
                  <td>{fmt.n(f.revenue / 1e8)}</td>
                  <td dangerouslySetInnerHTML={{ __html: fmt.pct(f.revenue_yoy) }} />
                  <td>{fmt.n(f.net_profit / 1e8)}</td>
                  <td dangerouslySetInnerHTML={{ __html: fmt.pct(f.net_profit_yoy) }} />
                  <td>{fmt.n(f.roe_weighted)}</td>
                  <td>{fmt.n(f.gross_margin)}</td>
                </tr>
              ))}
              {!d.financials.length && (
                <tr><td colSpan={8} className="l muted">暂无财报数据（请先执行数据更新）</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
