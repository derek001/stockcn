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
  params: {
    cover: number; cf_top: number; periods: number;
    cover_range: [number, number]; cf_top_range: [number, number];
    periods_range: [number, number];
    default_cover: number; default_cf_top: number; default_periods: number;
  };
  levels: F10Level[];
  totals: { n_codes: number; n_balance: number; n_cash: number; requests_est: number };
  stored: {
    built_at: string | null; cover: number | null; cf_top: number | null;
    revenue_periods: (string | null)[] | null;
    n_codes: number; added: number; removed: number;
  };
  // 按当前这套门槛重建名单之后还欠多少（库里已有那一期的就抵扣掉）
  if_rebuilt: { codes: number; units: number; dates: number; requests: number };
  backfilled: {
    n_codes: number; rows: number;
    todo_units: number; todo_codes: number; todo_dates: number; requests_left: number;
    // 被「问过但接口没回」记录压住的部分：清掉记录就恢复重试
    suppressed_dates: number; suppressed_requests: number; suppressed_codes: number;
    window: { first: string | null; last: string | null; n: number };
    confirm_at: number;
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

// 覆盖率在小数位上是 0.725 这类值，折成百分比只保留一位，别显示成 72.50000000000001%
function covLabel(v: number | null | undefined) {
  return v === null || v === undefined ? "-" : `${Math.round(v * 1000) / 10}%`;
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
  const [f10Force, setF10Force] = useState(false);  // 已入库的期也重抓一遍
  const [f10Cover, setF10Cover] = useState("");      // 负债表档覆盖率（%，如 70）
  const [f10CfTop, setF10CfTop] = useState("");      // 现金流档每行业前 N（只）
  const [f10Periods, setF10Periods] = useState("");  // 回填期数（季末报告期个数，20 = 5 年）
  const f10Used = useRef<{ cover: number; cfTop: number; periods: number } | null>(null);
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

  const loadF10 = (cover?: number, cfTop?: number, periods?: number): Promise<F10Preview | null> => {
    // 不传参就按「上一次预览用的门槛」重取；只有首屏（还没取过）才让后端按库里名单的口径算
    const use = cover !== undefined && cfTop !== undefined && periods !== undefined
      ? { cover, cfTop, periods } : f10Used.current;
    const qs = use ? `?cover=${use.cover}&cf_top=${use.cfTop}&periods=${use.periods}` : "";
    return api.get<F10Preview>("/api/data/f10/preview" + qs)
      .then((r) => {
        f10Used.current = { cover: r.params.cover, cfTop: r.params.cf_top, periods: r.params.periods };
        setF10pv(r);
        if (!use) {
          // 输入框回到库里名单实际用的那套门槛（保留一位小数，避免 72.5% 被折成 73%）
          setF10Cover(String(Math.round((r.stored.cover ?? r.params.default_cover) * 1000) / 10));
          setF10CfTop(String(r.stored.cf_top ?? r.params.default_cf_top));
          setF10Periods(String(r.params.periods));
        }
        return r;
      })
      .catch((e) => { setErr(String((e as Error).message)); return null; });
  };

  const f10CoverNum = Number(f10Cover);
  const f10CfTopNum = Number(f10CfTop);
  const f10PeriodsNum = Number(f10Periods);
  const covLo = Math.round((f10pv?.params.cover_range[0] ?? 0.3) * 100);
  const covHi = Math.round((f10pv?.params.cover_range[1] ?? 0.95) * 100);
  const cfLo = f10pv?.params.cf_top_range[0] ?? 3;
  const cfHi = f10pv?.params.cf_top_range[1] ?? 50;
  const pLo = f10pv?.params.periods_range[0] ?? 4;
  const pHi = f10pv?.params.periods_range[1] ?? 40;
  const f10Problems: string[] = [];
  if (!f10Cover || !Number.isFinite(f10CoverNum) || f10CoverNum < covLo || f10CoverNum > covHi)
    f10Problems.push(`资产负债表档覆盖率要填 ${covLo}%~${covHi}% 之间的数`);
  if (!f10CfTop || !Number.isInteger(f10CfTopNum) || f10CfTopNum < cfLo || f10CfTopNum > cfHi)
    f10Problems.push(`现金流量表档要填 ${cfLo}~${cfHi} 之间的整数（每行业营收前 N 只）`);
  if (!f10Periods || !Number.isInteger(f10PeriodsNum) || f10PeriodsNum < pLo || f10PeriodsNum > pHi)
    f10Problems.push(`回填期数要填 ${pLo}~${pHi} 之间的整数（4 期＝1 年）`);
  // 输入改了但还没点预览：下面的数字仍是上一次那套参数算出来的，得说清，不能让它看着像新结果
  const f10Dirty = !!f10pv && f10Problems.length === 0
    && (Math.abs(f10pv.params.cover * 100 - f10CoverNum) > 1e-9
      || f10pv.params.cf_top !== f10CfTopNum);
  // 期数是「开始回填」真正会用的值：改了没预览就按下，页面显示的请求数和实际发出去的不是一笔账，所以拦住
  const f10PeriodsDirty = !!f10pv && f10Problems.length === 0 && f10pv.params.periods !== f10PeriodsNum;

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
  // 待抓请求数一变（含改了期数、勾了重抓），之前那次勾选就作废（避免确认的是旧数字）
  useEffect(() => {
    setF10Confirm(false);
  }, [f10pv?.backfilled.requests_left, f10pv?.totals.requests_est, f10Force, f10Periods]);

  const runF10 = async () => {
    setErr(""); setF10Note("");
    try {
      const r = await api.post<{ codes: number; requests: number }>("/api/data/f10/run",
        { confirm: f10Confirm, periods: f10PeriodsNum, force: f10Force });
      setF10Confirm(false);
      setF10Note(`已开始回填：本次抓 ${r.codes} 只、${r.requests} 次请求（${f10PeriodsNum} 期），进度看上面进度条。`);
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const rebuildF10Sample = async () => {
    setErr(""); setF10Note("");
    try {
      await api.post("/api/data/f10/sample/rebuild",
        { cover: f10CoverNum / 100, cf_top: f10CfTopNum });
      const r = await loadF10(f10CoverNum / 100, f10CfTopNum, f10PeriodsNum);
      if (r) {
        setF10Note(`名单已按「覆盖率 ${f10Cover}% / 现金流前 ${f10CfTop}」重建：`
          + `${r.totals.n_codes} 只（资产负债表 ${r.totals.n_balance}、现金流量表 ${r.totals.n_cash}），`
          + `按 ${r.params.periods} 期算，重建后还欠 ${r.if_rebuilt.codes} 只 / ${r.if_rebuilt.units} 张表`
          + `（还缺 ${r.if_rebuilt.dates} 期）≈ ${r.if_rebuilt.requests} 次请求（点「开始回填」才会真的去抓）。`);
      }
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const clearF10 = async () => {
    setErr(""); setF10Note("");
    if (!window.confirm("清空会删掉已抓取的全部 F10 报表数据（样本名单保留，重试记录也一起清掉），确定继续？")) return;
    try {
      const r = await api.post<{ deleted_rows: number }>("/api/data/f10/clear");
      setF10Note(`已清空 ${r.deleted_rows} 行（「问过没回」的重试记录同时清掉，重抓不会被压成 0 次请求），`
        + "样本名单保留，可直接重新「开始回填」。");
      await loadF10();
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  const clearF10Attempts = async () => {
    setErr(""); setF10Note("");
    const s = f10pv?.backfilled;
    if (!s?.suppressed_dates) return;
    if (!window.confirm(`清掉「问过但接口没回」的重试记录？\n报表数据和名单都不动；`
      + `压住的 ${s.suppressed_dates} 期（${s.suppressed_codes} 只）会放回清单，`
      + `下次「开始回填」多发约 ${s.suppressed_requests} 次请求（点了才开始抓）。`)) return;
    try {
      const r = await api.post<{ cleared_periods: number }>("/api/data/f10/attempts/clear");
      const p = await loadF10();
      setF10Note(`已清掉 ${r.cleared_periods} 条重试记录`
        + (p ? `，现在「开始回填会抓」= ${p.backfilled.todo_codes} 只 / 缺 ${p.backfilled.todo_dates} 期`
              + `（约 ${p.backfilled.requests_left} 次请求），点「开始回填」才会真的去抓。` : "。"));
    } catch (e) {
      setErr(String((e as Error).message));
    }
  };

  if (!st) return <div className="panel">加载中… {pollErr || err}</div>;

  const busy = st.running.length > 0;
  // 勾了重抓就是「名单里每张表都按整个窗口重发」，抵扣全部作废，按全量预估算
  const f10Left = f10pv ? (f10Force ? f10pv.totals.requests_est : f10pv.backfilled.requests_left) : 0;
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
          怎么做：① 先看预览（名单只数、已抓、待抓请求数）；② 点「开始回填」抓数据（首轮全量约一小时，
          可随时离开页面），一次请求数超过 {f10pv?.backfilled.confirm_at ?? 200} 次时要先勾下面的确认框才能起；
          ③ 每天做完「在线数据 → 日常刷新」后点一次「重建名单」，新股会随最新财报进名单。
        </p>
        <p className="muted" style={{ marginTop: 0 }}>
          三个参数都可以调：负债表档＝每个行业营收累计盖到多少取最少只数；现金流档＝每个行业营收前 N 只；
          回填期数＝往回取几期季末报告（20 期＝5 年，想多要几年就调大，只会补多出来的那几期）。
          改完点「按这套参数预览」看新数字；门槛那两个输入框要点「重建名单」才写入名单（重建只改名单，不联网）。
          「开始回填」抓的是库里已存的名单，用的期数是输入框里那个值（改了期数得先预览一次核对请求数，按钮才按得下去）。
          默认档 70% / 前 10 / 20 期是实测够用的组合；门槛调小不会删掉已抓的数据，只是名单外的票
          不再进「行业库存周期」页的统计。
        </p>
        <p className="muted" style={{ marginTop: 0 }}>
          只请求过了法定披露截止日的报告期（年报与一季报 4-30、半年报 8-31、三季报 10-31），没到期那期不进清单，
          到期后下次点「开始回填」自动补上。抵扣按「哪只票的哪张表哪一期库里已有行」来算，缺哪几期就只发哪几期；
          怀疑某几只的数抓错了，勾「重抓已入库的期」整窗口重发（请求数会回到全量那一档）。
        </p>
        <p className="up" style={{ marginTop: 0 }}>
          注意：回填期间不要同时跑其他数据任务；中途断了或失败，直接再点一次「开始回填」会接着抓没完成的部分，
          已经入库的期不会重复请求，不用先清空。
        </p>
        <div className="row" style={{ marginTop: 8 }}>
          <div className="progress"><div style={{ width: `${pctOf(st.f10)}%` }} /></div>
          <span>{pctOf(st.f10)}%</span>
          <span className={st.f10.status === "error" ? "up" : "muted"}>{st.f10.message}</span>
        </div>
        <div className="row" style={{ marginTop: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span className="muted">负债表档覆盖率</span>
          <input style={{ width: 64 }} value={f10Cover} inputMode="decimal"
            placeholder={String(Math.round((f10pv?.params.default_cover ?? 0.7) * 1000) / 10)}
            onChange={(e) => setF10Cover(e.target.value.trim())} />
          <span className="muted">%（{covLo}~{covHi}）</span>
          <span className="muted" style={{ marginLeft: 10 }}>现金流档前</span>
          <input style={{ width: 52 }} value={f10CfTop} inputMode="numeric"
            placeholder={String(f10pv?.params.default_cf_top ?? 10)}
            onChange={(e) => setF10CfTop(e.target.value.trim())} />
          <span className="muted">只（{cfLo}~{cfHi}）</span>
          <span className="muted" style={{ marginLeft: 10 }}>回填期数</span>
          <input style={{ width: 52 }} value={f10Periods} inputMode="numeric"
            placeholder={String(f10pv?.params.default_periods ?? 20)}
            onChange={(e) => setF10Periods(e.target.value.trim())} />
          <span className="muted">期（{pLo}~{pHi}，4 期＝1 年）</span>
          <button className="btn" disabled={busy || f10Problems.length > 0}
            onClick={() => loadF10(f10CoverNum / 100, f10CfTopNum, f10PeriodsNum)}>按这套参数预览</button>
          <button className="btn" disabled={busy || f10Problems.length > 0}
            onClick={rebuildF10Sample}>重建名单</button>
        </div>
        {f10Problems.length > 0 && (
          <p className="up" style={{ margin: "4px 0 0" }}>
            {`参数不成立：${f10Problems.join("；")}。改对了才能预览和重建名单。`}
          </p>
        )}
        {f10Dirty && (
          <p className="up" style={{ margin: "4px 0 0" }}>
            {`输入框已改成「${f10Cover}% / 前 ${f10CfTop}」，但下面的数字还是「${covLabel(f10pv?.params.cover)}`
              + ` / 前 ${f10pv?.params.cf_top}」算的。点「按这套参数预览」看新数字，或点「重建名单」直接写入。`}
          </p>
        )}
        {f10PeriodsDirty && (
          <p className="up" style={{ margin: "4px 0 0" }}>
            {`回填期数已改成 ${f10Periods} 期，但下面的请求数还是按 ${f10pv?.params.periods} 期算的。`
              + `先点「按这套参数预览」核对数字，再点「开始回填」（回填用的就是这个输入框里的期数）。`}
          </p>
        )}
        <div className="row" style={{ marginTop: 8, alignItems: "flex-start", flexWrap: "wrap" }}>
          <button className="btn primary"
            disabled={busy || f10PeriodsDirty || f10NeedConfirm && !f10Confirm}
            onClick={runF10}>
            {st.f10.status === "running" ? "回填中…" : "开始回填"}
          </button>
          <label className="muted" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={f10Force} disabled={busy}
              onChange={(e) => setF10Force(e.target.checked)} />
            重抓已入库的期
          </label>
          <button className="btn" disabled={busy} onClick={clearF10}>清空已抓数据</button>
          <button className="btn" disabled={busy || !f10pv?.backfilled.suppressed_dates}
            onClick={clearF10Attempts}
            title={f10pv?.backfilled.suppressed_dates
              ? `清掉后这 ${f10pv.backfilled.suppressed_dates} 期放回清单，下次回填多发约 ${f10pv.backfilled.suppressed_requests} 次请求`
              : "当前没有「问过但接口没回」被压住的期，没什么可清"}>
            {f10pv?.backfilled.suppressed_dates
              ? `清掉重试记录（会重问 ${f10pv.backfilled.suppressed_requests} 次）`
              : "清掉重试记录（当前没有压住的期）"}
          </button>
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
              {`这次会向东财发起约 ${f10Left} 次请求，勾选后才能开始回填`}
            </label>
          </p>
        )}
        {f10Note && <p className="muted" style={{ marginTop: 6 }}>{f10Note}</p>}
        {f10pv && (
          <>
            <div className="row" style={{ marginTop: 8, alignItems: "flex-start", flexWrap: "wrap" }}>
              <span className="badge">
                {`库里名单: ${f10pv.stored.n_codes} 只（门槛 ${covLabel(f10pv.stored.cover)}`
                  + ` / 前 ${f10pv.stored.cf_top ?? "-"}，建于 ${f10pv.stored.built_at ?? "未记录"}）`}
              </span>
              <span className="badge">
                {`按这套门槛: ${f10pv.totals.n_codes} 只（资产负债表 ${f10pv.totals.n_balance}`
                  + `、现金流量表 ${f10pv.totals.n_cash}，全抓约 ${f10pv.totals.requests_est} 次请求）`}
              </span>
              <span className="badge">
                {`窗口: ${f10pv.backfilled.window.last ?? "-"} → ${f10pv.backfilled.window.first ?? "-"}`
                  + `（${f10pv.backfilled.window.n} 期，只算已过披露截止日的）`}
              </span>
              <span className={f10pv.if_rebuilt.units ? "badge up" : "badge"}>
                {`重建后还欠: ${f10pv.if_rebuilt.codes} 只 / ${f10pv.if_rebuilt.units} 张表`
                  + ` / 缺 ${f10pv.if_rebuilt.dates} 期（约 ${f10pv.if_rebuilt.requests} 次请求）`}
              </span>
              <span className="badge">已抓: {f10pv.backfilled.n_codes} 只 / {f10pv.backfilled.rows} 行</span>
              <span className={f10pv.backfilled.todo_units ? "badge up" : "badge"}>
                {`开始回填会抓: ${f10pv.backfilled.todo_codes} 只 / ${f10pv.backfilled.todo_units} 张表`
                  + ` / 缺 ${f10pv.backfilled.todo_dates} 期（约 ${f10pv.backfilled.requests_left} 次请求）`}
              </span>
              {!!f10pv.backfilled.suppressed_dates && (
                <span className="badge">
                  {`其中 ${f10pv.backfilled.suppressed_dates} 期问过没回，不再重复请求`
                    + `（${f10pv.backfilled.suppressed_codes} 只；清掉重试记录要重问 ${f10pv.backfilled.suppressed_requests} 次）`}
                </span>
              )}
              <button className="btn" onClick={() => setShowLevels((v) => !v)}>
                {showLevels ? "收起逐行业覆盖" : "看逐行业覆盖"}
              </button>
            </div>
            <p className="muted" style={{ marginTop: 4 }}>
              {`按「${covLabel(f10pv.params.cover)} / 前 ${f10pv.params.cf_top}」重算名单会有`
                + ` ${f10pv.stored.added} 只新增、${f10pv.stored.removed} 只退出（点「重建名单」写入）。`}
              {"「重建后还欠」= 换成这套门槛后还缺的表与期；「开始回填会抓」= 按库里现有名单算的欠账，两者不等说明名单该重建了。"}
              {"一张表缺 5 期以内算一次请求（接口一批最多带 5 期），所以请求数比分显示的「张表数」更贴近真实开销。"}
              {"「问过没回」= 上次抓的时候接口对这些报告期没返回数据（多是次新股上市前的期），记下来就不再白问；"}
              {"窗口里最新那期每轮都会再问一次，晚披露的能自己补上。点「清掉重试记录」恢复全量重试，报表数据和名单都不动。"}
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
