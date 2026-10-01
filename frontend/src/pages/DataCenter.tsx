import { useEffect, useRef, useState } from "react";
import { api } from "../api";

interface JobState { status: string; progress: number; total: number; message: string; time: string | null }
interface Status {
  full: JobState; incremental: JobState; local: JobState;
  provider: string | null; qmt_path: string | null;
  stock_count: number; running: string[]; last_full: string | null;
  last_incremental: string | null; last_local: string | null; kline_rows?: number;
}
interface Preview {
  path: string; files: number; matched: number; missing: number;
  missing_codes: string[]; missing_by_market: Record<string, number>;
  latest_date: string; warning: string;
}

function pctOf(j: JobState) {
  return j.total ? Math.round((j.progress / j.total) * 100) : j.status === "running" ? 5 : 0;
}

function Job({ label, j, busy, onStart }: {
  label: string; j: JobState; busy: boolean; onStart: () => void;
}) {
  const pct = pctOf(j);
  return (
    <div className="row">
      <button className="btn primary" disabled={busy} onClick={onStart}>
        {j.status === "running" ? "运行中…" : label}
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
  const [path, setPath] = useState("");
  const [pv, setPv] = useState<Preview | null>(null);
  const [force, setForce] = useState(false);
  const [copied, setCopied] = useState(false);
  const filled = useRef(false);

  useEffect(() => {
    const tick = () =>
      api.get<Status>("/api/data/status").then((s) => {
        setSt(s);
        // 只回填上一次通过体检/导入成功的那个目录，之后路径完全由用户掌握
        if (!filled.current) {
          filled.current = true;
          if (s.qmt_path) setPath(s.qmt_path);
        }
      }).catch((e) => setErr(String(e)));
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
    setErr(""); setPv(null); setCopied(false);
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

  const copyMissing = async () => {
    if (!pv) return;
    try {
      await navigator.clipboard.writeText(pv.missing_codes.join("\n"));
      setCopied(true);
    } catch (e) {
      setErr(`复制失败，请在下面的文本框里全选后手动复制：${String((e as Error).message)}`);
    }
  };

  if (!st) return <div className="panel">加载中… {err}</div>;

  const busy = st.running.length > 0;
  const rows = st.kline_rows ?? 0;
  const online = st.provider === "em" ? "东方财富" : st.provider === "tc" ? "腾讯财经" : "未探测";

  return (
    <>
      {err && <div className="panel up">{err}</div>}
      <div className="panel">
        <h3>数据概况</h3>
        <div className="row">
          <span className="badge">股票数: {st.stock_count}</span>
          <span className="badge">K线行数: {rows || "未导入"}</span>
          <span className={rows ? "badge" : "badge up"}>
            日线: {rows ? "QMT 本地导入" : "未导入，请在下方做一次全量导入"}
          </span>
          <span className="badge">其它数据来源: {online}</span>
        </div>
        <p className="muted">
          数据中心分两块，来源各自独立、互不覆盖：
          <strong>K线数据</strong>只来自本机 QMT 导出目录（<code>price_600000.txt</code>），
          在线接口不再下载任何个股日线，所以不存在两种复权口径混进同一只股票历史的时序问题；
          <strong>其它数据</strong>（股票列表、基本面快照、季度财报、行业板块、大盘指数）只来自在线接口。
          日常收盘后的顺序是：先跑「其它数据 → 日常刷新」拿到当日快照，再从 QMT 导出并「K线数据 → 增量导入」，
          导入时会用当日快照回填最后一根的涨跌幅、并体检导出数据是否为后复权。
        </p>
      </div>

      <div className="panel">
        <h3>K线数据（QMT 本地导入）</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          在 QMT「导出数据 → 日线」中按市场（SH/SZ/BJ）导出，目录内需为
          <code> price_600000.txt</code>（或 .csv）形式，表头
          <code> timetag,open,high,low,close,volumn,amount</code>；
          路径要填到<strong>含 SH/SZ/BJ 的那一层</strong>，QMT 常按复权口径再套一层目录。
          <span className="up">必须选择「后复权」</span>
          （本项目日线为后复权口径；不复权数据涨跌与分红送配不符，导入前会做体检拦截）。
          导出不含换手率，导入时按最新流通股本估算，并用最后一根日线补空快照的成交额/换手率。
          覆盖 A 股全部上市公司（沪深主板 / 创业板 / 科创板 / 北交所，不含 B 股、新三板与存托凭证）。
        </p>
        <div className="row">
          <input style={{ flex: 1, minWidth: 260 }} value={path}
            onChange={(e) => setPath(e.target.value)} placeholder="QMT 导出目录，填到含 SH/SZ/BJ 的那一层，如 E:\Project\QMT数据\后复权" />
          <button className="btn" disabled={busy || !path.trim()} onClick={check}>检查目录</button>
        </div>
        {pv && (
          <div className="row" style={{ marginTop: 8, alignItems: "flex-start" }}>
            <span className="badge">导出文件: {pv.files}</span>
            <span className="badge">匹配股票: {pv.matched}</span>
            <span className={pv.missing ? "badge up" : "badge"}>
              缺文件: {pv.missing}
              {pv.missing && Object.keys(pv.missing_by_market).length
                ? `（${Object.entries(pv.missing_by_market).map(([m, n]) => `${m} ${n}`).join(" / ")}）` : ""}
            </span>
            {pv.latest_date && <span className="badge">数据截止: {pv.latest_date}</span>}
            {pv.warning && <span className="up">{pv.warning}</span>}
          </div>
        )}
        {pv && pv.missing > 0 && (
          <div className="row" style={{ marginTop: 6, alignItems: "flex-start", flexWrap: "wrap" }}>
            <button className="btn" onClick={copyMissing}>复制缺文件清单</button>
            <span className="muted">{copied ? `已复制 ${pv.missing} 个代码` : "可回到 QMT 按清单补导出"}</span>
            <textarea
              readOnly
              value={pv.missing_codes.join("\n")}
              rows={6}
              style={{ flex: 1, minWidth: 260, fontFamily: "monospace", fontSize: 12 }}
            />
          </div>
        )}
        <label className="row" style={{ marginTop: 8 }}>
          <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)}
            style={{ width: "auto" }} />
          <span className="muted">强制导入：忽略「疑似不复权 / 盘中未结算 / 数据回退」体检拦截（覆盖率不足仍不允许导入）</span>
        </label>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={busy || !path.trim()} onClick={() => importLocal("full")}>
            {st.local.status === "running" ? "运行中…" : "全量导入（首次建库）"}
          </button>
          <button className="btn" disabled={busy || !path.trim()} onClick={() => importLocal("incremental")}>
            增量导入（日常）
          </button>
          <div className="progress"><div style={{ width: `${pctOf(st.local)}%` }} /></div>
          <span>{pctOf(st.local)}%</span>
          <span className={st.local.status === "error" ? "up" : "muted"}>{st.local.message}</span>
        </div>
        <p className="muted">
          全量导入用 QMT 数据整体替换本地日线库（写入暂存表后一次性换名，中断不损坏已有数据）；
          增量导入会重写各股票最后一根K线并追加更晚的日期（当天导错了，收盘结算后重导一次即可覆盖修正），
          需先完成一次全量导入。上次完成: {st.last_local ?? "从未"}
        </p>
      </div>

      <div className="panel">
        <h3>其它数据（在线）</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          只维护股票列表、基本面快照（现价/涨跌幅/市值/PE/PB/行业）、最近 12 期季度财报、行业板块与大盘指数，
          <strong>不下载个股日线</strong>；日线请走上面的 QMT 导入。
        </p>
        <Job label="全量刷新（首次建库）" j={st.full} busy={busy} onStart={() => start("full")} />
        <p className="muted">拉取全部在册股票的列表与快照、近 12 期财报、板块和指数。上次完成: {st.last_full ?? "从未"}</p>
        <Job label="日常刷新" j={st.incremental} busy={busy} onStart={() => start("incremental")} />
        <p className="muted">
          只刷新列表与快照、近 2 期财报、指数，日线库一行不动。上次完成: {st.last_incremental ?? "从未"}
        </p>
      </div>
    </>
  );
}
