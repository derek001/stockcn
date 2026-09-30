import { useEffect, useState } from "react";
import { api } from "../api";

interface JobState { status: string; progress: number; total: number; message: string; time: string | null }
interface Status {
  full: JobState; incremental: JobState; local: JobState;
  provider: string | null; kline_source: string | null;
  stock_count: number; running: string[]; last_full: string | null;
  last_incremental: string | null; last_local: string | null; kline_rows?: number;
}
interface Preview {
  path: string; files: number; matched: number; missing: number;
  sample_missing: string[]; latest_date: string; warning: string;
}

const DEFAULT_PATH = "E:\\Project\\QMT数据";

function Job({ label, j, busy, onStart }: {
  label: string; j: JobState; busy: boolean; onStart: () => void;
}) {
  const running = j.status === "running";
  const pct = j.total ? Math.round((j.progress / j.total) * 100) : running ? 5 : 0;
  return (
    <div className="row">
      <button className="btn primary" disabled={busy} onClick={onStart}>
        {running ? "运行中…" : label}
      </button>
      <div className="progress"><div style={{ width: `${pct}%` }} /></div>
      <span>{pct}%</span>
      <span className={j.status === "error" ? "up" : "muted"}>{j.message}</span>
    </div>
  );
}

export default function DataCenter() {
  const [st, setSt] = useState<Status | null>(null);
  const [err, setErr] = useState("");
  const [path, setPath] = useState(DEFAULT_PATH);
  const [pv, setPv] = useState<Preview | null>(null);
  const [force, setForce] = useState(false);

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

  const check = async () => {
    setErr(""); setPv(null);
    try {
      setPv(await api.post<Preview>("/api/data/qmt/preview", { path }));
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const importLocal = async (mode: "full" | "incremental") => {
    setErr("");
    if (mode === "full" && !window.confirm(
      "全量导入会用 QMT 数据整体替换本地日线库（导入期间不影响查询），确定继续？")) return;
    try {
      await api.post("/api/data/qmt/import", { path, mode, force });
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  if (!st) return <div className="panel">加载中… {err}</div>;

  const busy = st.running.length > 0;
  const source = st.kline_source === "qmt" ? "QMT 本地导入"
    : st.kline_source === "remote" ? (st.provider === "em" ? "东方财富" : "腾讯财经") : "未导入";

  return (
    <>
      {err && <div className="panel up">{err}</div>}
      <div className="panel">
        <h3>数据概况</h3>
        <div className="row">
          <span className="badge">股票数: {st.stock_count}</span>
          <span className="badge">K线行数: {st.kline_rows ?? "-"}</span>
          <span className="badge">日线来源: {source}</span>
          <span className="badge">K线类型: 日线（后复权，可扩展周线/月线）</span>
        </div>
        <p className="muted">
          数据覆盖 A 股全部上市公司（沪深主板 / 创业板 / 科创板 / 北交所，不含 B 股与新三板）的日线 K 线
          与基本面快照（市值/PE/PB/行业）、最近 12 期季度财报。
          首次使用请任选一种方式建库：「全量更新」在线下载（耗时数小时），或「本地导入 QMT 行情数据」
          （推荐，读取本机 QMT 导出目录，分钟级完成且含成交额）；日常用「增量更新」或「增量导入」补齐到最新交易日。
          在线源与 QMT 的后复权基准不同，两者不可混用。
        </p>
      </div>
      <div className="panel">
        <h3>本地导入 QMT 行情数据</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          在 QMT「导出数据 → 日线」中按市场（SH/SZ/BJ）导出，目录内需为
          <code> price_600000.txt</code>（或 .csv）形式，表头
          <code> timetag,open,high,low,close,volumn,amount</code>。
          <span className="up">必须选择「后复权」</span>
          （本项目日线为后复权口径；不复权数据涨跌与分红送配不符，导入前会做体检拦截）。
          QMT 导出<strong>只覆盖日线K线</strong>：股票列表、基本面快照、季度财报、指数K线仍由
          「全量/增量更新」从在线接口维护，导入任务不会碰这些表。
          导出不含换手率，导入时按最新流通股本估算，并用最后一根日线补空快照的成交额/换手率。
        </p>
        <div className="row">
          <input style={{ flex: 1, minWidth: 260 }} value={path}
            onChange={(e) => setPath(e.target.value)} placeholder="QMT 导出目录，如 E:\Project\QMT数据" />
          <button className="btn" disabled={busy} onClick={check}>检查目录</button>
        </div>
        {pv && (
          <div className="row" style={{ marginTop: 8 }}>
            <span className="badge">导出文件: {pv.files}</span>
            <span className="badge">匹配股票: {pv.matched}</span>
            <span className="badge">缺文件: {pv.missing}
              {pv.missing > 0 && pv.sample_missing.length ? `（如 ${pv.sample_missing.slice(0, 5).join("、")}）` : ""}
            </span>
            {pv.latest_date && <span className="badge">数据截止: {pv.latest_date}</span>}
            {pv.warning && <span className="up">{pv.warning}</span>}
          </div>
        )}
        <label className="row" style={{ marginTop: 8 }}>
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)}
            style={{ width: "auto" }} />
          <span className="muted">强制导入：忽略「疑似不复权 / 盘中未结算 / 数据回退」体检拦截（覆盖率不足仍不允许导入）</span>
        </label>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={busy} onClick={() => importLocal("full")}>
            {st.local.status === "running" ? "运行中…" : "全量导入"}
          </button>
          <button className="btn" disabled={busy} onClick={() => importLocal("incremental")}>
            增量导入
          </button>
          <div className="progress"><div style={{ width: `${
            st.local.total
              ? Math.round((st.local.progress / st.local.total) * 100)
              : st.local.status === "running" ? 5 : 0}%` }} /></div>
          <span className={st.local.status === "error" ? "up" : "muted"}>{st.local.message}</span>
        </div>
        <p className="muted">
          全量导入用 QMT 数据整体替换本地日线库（写入暂存表后一次性换名，中断不损坏已有数据）；
          增量导入会重写各股票最后一根K线并追加更晚的日期（当天导错了，收盘结算后重导一次即可覆盖修正），
          需先完成一次全量导入。上次完成: {st.last_local ?? "从未"}
        </p>
      </div>
      <div className="panel">
        <h3>全量更新</h3>
        <p className="muted" style={{ marginTop: 0 }}>在线下载所有股票自上市以来的全部数据到本地数据库。</p>
        <Job label="开始全量更新" j={st.full} busy={busy} onStart={() => start("full")} />
        <p className="muted">上次完成: {st.last_full ?? "从未"}</p>
      </div>
      <div className="panel">
        <h3>增量更新</h3>
        <p className="muted" style={{ marginTop: 0 }}>在线刷新股票列表与财报、指数；本地已有股票的日线补齐到最新交易日（后复权口径）。日线库来自 QMT 导入时只刷新列表/财报/指数，日线请改用「增量导入」。</p>
        <Job label="开始增量更新" j={st.incremental} busy={busy} onStart={() => start("incremental")} />
        <p className="muted">上次完成: {st.last_incremental ?? "从未"}</p>
      </div>
    </>
  );
}
