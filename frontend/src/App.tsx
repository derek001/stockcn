import { useState } from "react";
import DataCenter from "./pages/DataCenter";
import Pools from "./pages/Pools";
import StockDetail from "./pages/StockDetail";
import Screener from "./pages/Screener";
import AutoScreener from "./pages/AutoScreener";
import Selectors from "./pages/Selectors";
import Strategies from "./pages/Strategies";
import Backtest, { DrillDown } from "./pages/Backtest";
import BatchBacktest from "./pages/BatchBacktest";

type View =
  | { page: "data" }
  | { page: "pools" }
  | { page: "stock"; code: string }
  | { page: "manual" }
  | { page: "auto" }
  | { page: "selector" }
  | { page: "strategies" }
  | { page: "backtest"; code?: string; from?: DrillDown }
  | { page: "batch" };

const NAV: { key: View["page"]; label: string }[] = [
  { key: "data", label: "数据中心" },
  { key: "pools", label: "股票自选池" },
  { key: "manual", label: "手动选股" },
  { key: "auto", label: "自动选股" },
  { key: "selector", label: "策略选股" },
  { key: "strategies", label: "交易策略" },
  { key: "backtest", label: "回测" },
  { key: "batch", label: "全市场回测" },
];

export default function App() {
  const [view, setView] = useState<View>({ page: "data" });

  const nav = (p: View["page"]) => setView({ page: p } as View);

  return (
    <>
      <div className="topbar">
        <span className="logo">A股分析终端</span>
        {NAV.map((n) => (
          <button
            key={n.key}
            className={`tab ${
              (view.page === "stock" && n.key === "pools") || view.page === n.key
                ? "active"
                : ""
            }`}
            onClick={() => nav(n.key)}
          >
            {n.label}
          </button>
        ))}
      </div>
      <div className="page">
        {view.page === "data" && <DataCenter />}
        {view.page === "pools" && <Pools onOpenStock={(c) => setView({ page: "stock", code: c })} />}
        {view.page === "stock" && (
          <StockDetail code={view.code} onBack={() => setView({ page: "pools" })}
            onBacktest={() => setView({ page: "backtest", code: view.code })} />
        )}
        {view.page === "manual" && <Screener onOpenStock={(c) => setView({ page: "stock", code: c })} />}
        {view.page === "auto" && <AutoScreener onOpenStock={(c) => setView({ page: "stock", code: c })} />}
        {view.page === "selector" && <Selectors onOpenStock={(c) => setView({ page: "stock", code: c })} />}
        {view.page === "strategies" && <Strategies />}
        {view.page === "backtest" && <Backtest initialCode={view.code} from={view.from} />}
        {view.page === "batch" && (
          <BatchBacktest onBacktest={(d) => setView({ page: "backtest", from: d })} />
        )}
      </div>
    </>
  );
}
