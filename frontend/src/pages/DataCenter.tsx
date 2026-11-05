import { useEffect, useRef, useState } from "react";
import { api } from "../api";

interface JobState { status: string; progress: number; total: number; message: string; time: string | null }
interface Status {
  full: JobState; incremental: JobState; local: JobState; f10: JobState;
  provider: string | null;
  qmt_path_raw: string | null; qmt_path_geo: string | null;
  stock_count: number; running: string[]; last_full: string | null;
  last_incremental: string | null; last_local: string | null; last_f10: string | null;
  kline_rows?: number;
  orphan_tables?: string[];
}
interface F10Level {
  level1: string; n_rev: number; revenue_yi: number; finance: boolean;
  n_balance: number; n_cash: number; cover_pct: number | null;
}
interface F10Preview {
  levels: F10Level[];
  totals: { n_codes: number; n_balance: number; n_cash: number; requests_est: number };
  stored: {
    built_at: string | null; revenue_periods: (string | null)[] | null;
    n_codes: number; added: number; removed: number;
  };
  backfilled: {
    n_codes: number; rows: number;
    todo_units: number; todo_codes: number; requests_left: number; confirm_at: number;
  };
}
interface Preview {
  raw_path: string; geo_path: string;
  raw_files: number; geo_files: number; matched: number; missing: number;
  missing_codes: string[]; missing_by_market: Record<string, number>;
  raw_latest: string; geo_latest: string; latest_date: string;
  sample_stocks: number; factor_events: number; factor_unreproduced: number;
  unmatched_rows: number; warning: string;
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
  const [pathRaw, setPathRaw] = useState("");
  const [pathGeo, setPathGeo] = useState("");
  const [pv, setPv] = useState<Preview | null>(null);
  const [force, setForce] = useState(false);
  const [copied, setCopied] = useState(false);
  const [f10pv, setF10pv] = useState<F10Preview | null>(null);
  const [f10Note, setF10Note] = useState("");
  const [f10Confirm, setF10Confirm] = useState(false);
  const [showLevels, setShowLevels] = useState(false);
  const [pollErr, setPollErr] = useState("");
  const filled = useRef(false);

  useEffect(() => {
    const tick = () =>
      api.get<Status>("/api/data/status").then((s) => {
        setSt(s);
        setPollErr("");
        // 只回填上一次通过体检/导入成功的那两个目录，之后路径完全由用户掌握
        if (!filled.current) {
          filled.current = true;
          if (s.qmt_path_raw) setPathRaw(s.qmt_path_raw);
          if (s.qmt_path_geo) setPathGeo(s.qmt_path_geo);
        }
      }).catch((e) => setPollErr(String(e)));
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

  const bothFilled = pathRaw.trim().length > 0 && pathGeo.trim().length > 0;
  const paths = { raw_path: pathRaw.trim(), geo_path: pathGeo.trim() };

  const check = async () => {
    setErr(""); setPv(null); setCopied(false);
    try {
      setPv(await api.post<Preview>("/api/data/qmt/preview", paths));
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const importLocal = async (mode: "full" | "incremental") => {
    setErr("");
    if (mode === "full" && !window.confirm(
      "全量导入会用 QMT 数据整体替换本地日线库（导入期间不影响查询），确定继续？")) return;
    try {
      await api.post("/api/data/qmt/import", { ...paths, mode, force });
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

  const loadF10 = () =>
    api.get<F10Preview>("/api/data/f10/preview")
      .then(setF10pv)
      .catch((e) => setErr(String((e as Error).message)));

  const f10WasRunning = useRef(false);
  useEffect(() => {
    loadF10();
  }, []);
  useEffect(() => {
    if (!st) return;
    const r = st.running.includes("f10");
    // 回填跑完再取一次：已抓行数、待抓只数只有落库后才准
    if (f10WasRunning.current && !r) loadF10();
    f10WasRunning.current = r;
  }, [st]);
  // 待抓请求数一变，之前那次勾选就作废（避免确认的是旧数字）
  useEffect(() => {
    setF10Confirm(false);
  }, [f10pv?.backfilled.requests_left]);

  const runF10 = async () => {
    setErr(""); setF10Note("");
    try {
      const r = await api.post<{ codes: number; requests: number }>("/api/data/f10/run",
        { confirm: f10Confirm });
      setF10Confirm(false);
      setF10Note(`已开始回填：本次抓 ${r.codes} 只，约 ${r.requests} 次请求，进度看上面进度条。`);
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const rebuildF10Sample = async () => {
    setErr("");
    try {
      await api.post("/api/data/f10/sample/rebuild");
      await loadF10();
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const clearF10 = async () => {
    setErr(""); setF10Note("");
    if (!window.confirm("清空会删掉已抓取的全部 F10 报表数据（样本名单保留），确定继续？")) return;
    try {
      const r = await api.post<{ deleted_rows: number }>("/api/data/f10/clear");
      setF10Note(`已清空 ${r.deleted_rows} 行，样本名单保留，可直接重新「开始回填」。`);
      await loadF10();
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  if (!st) return <div className="panel">加载中… {pollErr || err}</div>;

  const busy = st.running.length > 0;
  const f10Left = f10pv?.backfilled.requests_left ?? 0;
  const f10NeedConfirm = !!f10pv && f10Left > f10pv.backfilled.confirm_at;
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
            本地数据: {rows ? "日线已导入" : "未导入，请在下方「本地数据」做一次全量导入"}
          </span>
          <span className="badge">在线数据: {online}</span>
        </div>
        {pollErr && (
          <p className="up" style={{ margin: "6px 0 0" }}>
            状态读取失败：{pollErr}（服务重启或未运行时会自动重试，无需刷新页面）
          </p>
        )}
        <p className="muted">
          数据分两块，两块都重要、来源各自独立互不覆盖：
          <strong>本地数据</strong>是从本机 QMT 导出文件读进来的个股日线，
          <strong>在线数据</strong>是从公开接口拉的股票列表、行情快照、财报、板块和指数。
          每个交易日收盘后按三步走：① 「在线数据 → 日常刷新」→ ② 在 QMT 把当日日线导成两份 → ③ 回「本地数据 → 增量导入」。
        </p>
      </div>

      <div className="panel">
        <h3>在线数据（公开接口）</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          包含：股票列表（含每只的上市日期，个股页可看）、最新价/涨跌幅/市值/PE/PB/行业的当日快照、最近几个季度的财报、行业板块、大盘指数，
          <strong>不含个股日线</strong>（日线在下面的「本地数据」里）。平时点「日常刷新」就够了。
        </p>
        <Job label="全量刷新（首次建库）" j={st.full} busy={busy} onStart={() => start("full")} />
        <p className="muted">首次建库用，会把近 12 期财报全部拉一遍，耗时较长。上次完成: {st.last_full ?? "从未"}</p>
        <Job label="日常刷新" j={st.incremental} busy={busy} onStart={() => start("incremental")} />
        <p className="muted">
          每天收盘后先点这个（它只更新列表与快照、近 2 期财报、指数，不动日线库）。上次完成: {st.last_incremental ?? "从未"}
        </p>
      </div>

      <div className="panel">
        <h3>本地数据（QMT 导入）</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          包含：个股日线（开高低收、成交量/成交额、涨跌幅），覆盖 A 股全部上市公司，只从下面这两个 QMT 目录进库。
        </p>
        <p className="muted" style={{ marginTop: 0 }}>
          怎么做：① 收盘结算后（建议 18:00 之后）在 QMT「导出数据 → 日线」按市场（SH/SZ/BJ）导出
          <strong>同一天的两份</strong>，一份选<span className="up">「不复权」</span>、一份选<span className="up">「等比后复权」</span>；
          ② 在下面填上这两个目录（<strong>填到含 SH/SZ/BJ 的那一层</strong>）；③ 点「检查目录」看体检结果；
          ④ 首次建库点「全量导入」，平时点「增量导入」。
        </p>
        <p className="up" style={{ marginTop: 0 }}>
          注意：只认「不复权 + 等比后复权」这一对，<strong>不要选「后复权」，也不要选「前复权 / 等比前复权」</strong>；
          两份必须是同一天导的。盘中导出的话当天那根数据不全，等结算后重导一次即可（增量导入会重写最新一天，不会留下错数据）。
        </p>
        {!!st.orphan_tables?.length && (
          <p className="up" style={{ marginTop: 0 }}>
            {`上一次「全量导入」中途断了：库里还留着中转表 ${st.orphan_tables.join(" / ")}，`
              + "它和整份日线库一样大、正占着磁盘。请在下面重跑一次「全量导入」，跑完会自动清掉。"}
          </p>
        )}
        <div className="row">
          <span style={{ width: 92 }} className="muted">不复权目录</span>
          <input style={{ flex: 1, minWidth: 260 }} value={pathRaw}
            onChange={(e) => setPathRaw(e.target.value)} placeholder="QMT「不复权」导出目录，填到含 SH/SZ/BJ 的那一层，如 E:\Project\QMT数据\不复权" />
        </div>
        <div className="row" style={{ marginTop: 6 }}>
          <span style={{ width: 92 }} className="muted">等比后复权</span>
          <input style={{ flex: 1, minWidth: 260 }} value={pathGeo}
            onChange={(e) => setPathGeo(e.target.value)} placeholder="QMT「等比后复权」导出目录，同一交易日，如 E:\Project\QMT数据\等比后复权" />
          <button className="btn" disabled={busy || !bothFilled} onClick={check}>检查目录</button>
        </div>
        {pv && (
          <>
            <div className="row" style={{ marginTop: 8, alignItems: "flex-start" }}>
              <span className="badge">不复权文件: {pv.raw_files}</span>
              <span className="badge">等比文件: {pv.geo_files}</span>
              <span className="badge">配对成功: {pv.matched}</span>
              <span className={pv.missing ? "badge up" : "badge"}>
                缺文件: {pv.missing}
                {pv.missing && Object.keys(pv.missing_by_market).length
                  ? `（${Object.entries(pv.missing_by_market).map(([m, n]) => `${m} ${n}`).join(" / ")}）` : ""}
              </span>
              {pv.raw_latest && <span className="badge">不复权截止: {pv.raw_latest}</span>}
              {pv.geo_latest && <span className="badge">等比截止: {pv.geo_latest}</span>}
            </div>
            <div className="row" style={{ marginTop: 4, alignItems: "flex-start" }}>
              <span className={pv.warning ? "up" : "muted"}>
                {pv.warning
                  ? "体检发现问题，请先照下面的提示回 QMT 重导："
                  : `体检通过（抽检 ${pv.sample_stocks} 只股票），口径一致，可以导入。`}
              </span>
              {pv.warning && <span className="up">{pv.warning}</span>}
            </div>
          </>
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
          <span className="muted">强制导入：确认过体检提示没问题，仍要导入（缺文件的股票太多时这条不生效）</span>
        </label>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={busy || !bothFilled} onClick={() => importLocal("full")}>
            {st.local.status === "running" ? "运行中…" : "全量导入（首次建库）"}
          </button>
          <button className="btn" disabled={busy || !bothFilled} onClick={() => importLocal("incremental")}>
            增量导入（日常）
          </button>
          <div className="progress"><div style={{ width: `${pctOf(st.local)}%` }} /></div>
          <span>{pctOf(st.local)}%</span>
          <span className={st.local.status === "error" ? "up" : "muted"}>{st.local.message}</span>
        </div>
        <p className="muted">
          「全量导入」= 用这两个目录整体重建本地日线（首次建库或怀疑历史数据不对时才用，5573 只约 5 分钟，导入期间照常查询）；
          「增量导入」= 只补最新几天，日常用它。上次完成: {st.last_local ?? "从未"}
        </p>
      </div>

      <div className="panel">
        <h3>F10 报表数据（东财：存货与资本开支）</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          包含：样本名单里每只股票的资产负债表（存货、在建工程、固定资产、合同负债等）
          和现金流量表（购建长期资产支付的现金、经营现金流），按季度逐期存进本地库，用来做行业库存与资本开支的横向对比。
          名单只覆盖各行业营收占比靠前的一部分股票，不是全市场。
        </p>
        <p className="muted" style={{ marginTop: 0 }}>
          怎么做：① 点「刷新预览」看名单和已抓情况；② 点「开始回填」抓数据（约一小时，可随时离开页面）；
          一次请求数超过 {f10pv?.backfilled.confirm_at ?? 200} 次时要先勾下面的确认框才能起；
          ③ 每天做完「在线数据 → 日常刷新」后，名单会随最新财报变化，先点「重建名单」再回填新增的票。
        </p>
        <p className="up" style={{ marginTop: 0 }}>
          注意：回填期间不要同时跑其他数据任务；中途断了或失败，直接再点一次「开始回填」会接着抓没完成的股票，
          已抓过的不会重复请求（要全部重抓请点「清空已抓数据」后重跑）。
        </p>
        <div className="row" style={{ marginTop: 8 }}>
          <div className="progress"><div style={{ width: `${pctOf(st.f10)}%` }} /></div>
          <span>{pctOf(st.f10)}%</span>
          <span className={st.f10.status === "error" ? "up" : "muted"}>{st.f10.message}</span>
        </div>
        <div className="row" style={{ marginTop: 8, alignItems: "flex-start", flexWrap: "wrap" }}>
          <button className="btn" onClick={loadF10}>刷新预览</button>
          <button className="btn" disabled={busy} onClick={rebuildF10Sample}>重建名单</button>
          <button className="btn primary" disabled={busy || f10NeedConfirm && !f10Confirm} onClick={runF10}>
            {st.f10.status === "running" ? "回填中…" : "开始回填"}
          </button>
          <button className="btn" disabled={busy} onClick={clearF10}>清空已抓数据</button>
          <span className="muted">上次完成: {st.last_f10 ?? "从未"}</span>
        </div>
        {f10NeedConfirm && (
          <p className="up" style={{ margin: "6px 0 0" }}>
            <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={f10Confirm}
                disabled={busy}
                onChange={(e) => setF10Confirm(e.target.checked)}
              />
              {`这次会向东财发起约 ${f10pv?.backfilled.requests_left ?? 0} 次请求，勾选后才能开始回填`}
            </label>
          </p>
        )}
        {f10Note && <p className="muted" style={{ marginTop: 6 }}>{f10Note}</p>}
        {f10pv && (
          <>
            <div className="row" style={{ marginTop: 8, alignItems: "flex-start", flexWrap: "wrap" }}>
              <span className="badge">名单: {f10pv.stored.n_codes} 只</span>
              <span className="badge">资产负债表: {f10pv.totals.n_balance} 只</span>
              <span className="badge">现金流量表: {f10pv.totals.n_cash} 只</span>
              <span className="badge">预计请求: 约 {f10pv.totals.requests_est} 次</span>
              <span className="badge">已抓: {f10pv.backfilled.n_codes} 只 / {f10pv.backfilled.rows} 行</span>
              <span className={f10pv.backfilled.todo_units ? "badge up" : "badge"}>
                {`待抓: ${f10pv.backfilled.todo_codes} 只 / ${f10pv.backfilled.todo_units} 张表`
                  + `（约 ${f10pv.backfilled.requests_left} 次请求）`}
              </span>
              <button className="btn" onClick={() => setShowLevels((v) => !v)}>
                {showLevels ? "收起逐行业覆盖" : "看逐行业覆盖"}
              </button>
            </div>
            <p className="muted" style={{ marginTop: 4 }}>
              {`名单按最新财报营收重算后有 ${f10pv.stored.added} 只新增、${f10pv.stored.removed} 只退出`
                + "（点「重建名单」写入）。"}
            </p>
            {showLevels && (
              <div className="table-wrap" style={{ marginTop: 8, maxHeight: 420 }}>
                <table>
                  <thead>
                    <tr>
                      <th>行业</th><th>有营收的只数</th><th>行业营收(亿)</th>
                      <th>资产负债表只数</th><th>营收覆盖率</th><th>现金流量表只数</th>
                    </tr>
                  </thead>
                  <tbody>
                    {f10pv.levels.map((l) => (
                      <tr key={l.level1} title={l.finance ? "银行/非银金融报表口径不同，整体不参与" : undefined}>
                        <td>{l.level1}</td>
                        <td>{l.n_rev}</td>
                        <td>{l.revenue_yi.toFixed(0)}</td>
                        <td>{l.finance ? "-" : l.n_balance}</td>
                        <td>{l.finance ? "-" : `${l.cover_pct}%`}</td>
                        <td>{l.finance ? "-" : l.n_cash}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>

    </>
  );
}
