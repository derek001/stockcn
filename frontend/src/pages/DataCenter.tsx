import { useEffect, useState } from "react";
import { api } from "../api";

interface JobState { status: string; progress: number; total: number; message: string; time: string | null }
interface Status {
  full: JobState; incremental: JobState; provider: string | null;
  stock_count: number; running: string[]; last_full: string | null;
  last_incremental: string | null; kline_rows?: number;
}

export default function DataCenter() {
  const [st, setSt] = useState<Status | null>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const tick = () =>
      api.get<Status>("/api/data/status").then(setSt).catch((e) => setErr(String(e)));
    tick();
    const id = setInterval(tick, 2000);
    return () => clearInterval(id);
  }, []);

  const start = async (kind: "full" | "incremental") => {
    setErr("");
    try {
      await api.post(`/api/data/${kind}`);
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  if (!st) return <div className="panel">加载中… {err}</div>;

  const Job = ({ kind, label, desc }: { kind: "full" | "incremental"; label: string; desc: string }) => {
    const j = st[kind];
    const running = j.status === "running";
    const pct = j.total ? Math.round((j.progress / j.total) * 100) : running ? 5 : 0;
    return (
      <div className="panel">
        <h3>{label}</h3>
        <p className="muted" style={{ marginTop: 0 }}>{desc}</p>
        <div className="row">
          <button className="btn primary" disabled={st.running.length > 0}
            onClick={() => start(kind)}>
            {running ? "运行中…" : `开始${label}`}
          </button>
          <div className="progress"><div style={{ width: `${pct}%` }} /></div>
          <span>{pct}%</span>
          <span className={j.status === "error" ? "up" : "muted"}>{j.message}</span>
        </div>
        <p className="muted">上次完成: {j.time ?? "从未"}</p>
      </div>
    );
  };

  return (
    <>
      {err && <div className="panel up">{err}</div>}
      <div className="panel">
        <h3>数据概况</h3>
        <div className="row">
          <span className="badge">股票数: {st.stock_count}</span>
          <span className="badge">K线行数: {st.kline_rows ?? "-"}</span>
          <span className="badge">当前数据源: {st.provider === "em" ? "东方财富" : st.provider === "tc" ? "腾讯财经" : "未初始化"}</span>
          <span className="badge">K线类型: 日线（后复权，可扩展周线/月线）</span>
        </div>
        <p className="muted">
          数据覆盖 A 股全部上市公司的日线 K 线与基本面快照（市值/PE/PB/行业）、最近 12 期季度财报。
          首次使用请点击「全量更新」（耗时较长，可参考页面提示进度）；日常使用「增量更新」即可补齐到最新交易日。
        </p>
      </div>
      <Job kind="full" label="全量更新" desc="下载所有股票自上市以来的全部数据到本地数据库。" />
      <Job kind="incremental" label="增量更新" desc="将本地数据补齐到最新交易日（后复权口径，历史数据稳定）。" />
    </>
  );
}
