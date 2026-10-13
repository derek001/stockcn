import { useEffect, useState } from "react";
import { api, fmt } from "../api";

interface ParamDef {
  key: string; label: string; type: string; default: number; min?: number; max?: number;
}
interface Attrs { [k: string]: string | number | null }
interface HitRow { code: string; score: number; reason: string; attrs: Attrs }
interface Meta {
  id: string; name: string; description: string; params_schema: ParamDef[];
  enabled: boolean; params: Record<string, number>;
  last?: { trade_date: string; count: number; run_at: string };
}
interface History {
  selector_id: string; trade_date: string; params: Record<string, number>;
  count: number; rows: HitRow[]; run_at?: string; dates?: string[];
}
interface ReplayTrade {
  code: string; name: string; base: string; entry_date: string; exit_date: string;
  days: number; ret_pct: number; bh_pct: number; excess_idx_pct: number | null;
  exit_reason: string;
}
interface ReplayAgg {
  n: number; win_rate: number | null; ret_med: number | null; ret_mean: number | null;
  ret_p25: number | null; ret_p75: number | null; excess_own_med: number | null;
  excess_idx_med: number | null; stop_rate: number | null; timeout_rate: number | null;
}
interface ReplayCombo {
  gates: Record<string, number>; agg: ReplayAgg;
  snapshots: { base: string; hits: number; trades: number; no_entry: number; broken: number }[];
  trades: ReplayTrade[];
}
interface ReplayResult {
  job_id: string; elapsed_sec: number; bases: string[];
  combos: ReplayCombo[];
}
interface ReplayStatus {
  status: string; progress: number; total: number; message: string;
  job_id?: string;
  result?: ReplayResult;
}

// 门槛标签从 params_schema 派生（截到 12 字），schema 加参数不用同步前端映射
function schemaLabels(meta?: Meta): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of meta?.params_schema ?? [])
    out[p.key] = p.label.split(/[（(]/)[0].trim().slice(0, 12);
  return out;
}

function gatesDiff(base: Record<string, number>, cur: Record<string, number>,
                   labels: Record<string, string>): string {
  const parts: string[] = [];
  for (const k of Object.keys(cur)) {
    if (base[k] !== cur[k]) parts.push(`${labels[k] ?? k}=${cur[k]}`);
  }
  return parts.length ? parts.join("，") : "当前门槛";
}

export default function Selectors({ onOpenStock }: { onOpenStock: (c: string) => void }) {
  const [metas, setMetas] = useState<Meta[] | null>(null);
  const [draft, setDraft] = useState<Record<string, Record<string, string>>>({});
  const [hist, setHist] = useState<Record<string, History>>({});
  const [checked, setChecked] = useState<Record<string, Set<string>>>({});
  const [pools, setPools] = useState<{ id: number; name: string }[]>([]);
  const [poolId, setPoolId] = useState(0);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<Record<string, string>>({});
  const [rp, setRp] = useState<ReplayStatus | null>(null);
  const [rpErr, setRpErr] = useState("");
  const [rpForm, setRpForm] = useState({ snapshots: "6", step_bars: "60", hold_bars: "250", entry: "10", exit: "10", stop: "5" });
  const [rpSens, setRpSens] = useState(true);
  const [rpCombo, setRpCombo] = useState(0);

  const loadReplay = () =>
    api.get<ReplayStatus>("/api/selectors/box/replay").then(setRp).catch(() => { /* 后端没起或没跑过 */ });

  const startReplay = async () => {
    setRpErr("");
    const meta = metas?.find((m) => m.id === "box_range");
    if (!meta) return;
    const gates: Record<string, number> = {};
    for (const p of meta.params_schema)
      gates[p.key] = Number(draft["box_range"]?.[p.key] ?? p.default);
    const grid = rpSens
      ? [{ ...gates, ext_max: Math.min(200, gates.ext_max + 50) },
         { ...gates, decline_min: Math.max(0, gates.decline_min - 20) }]
      : [];
    try {
      await api.post("/api/selectors/box/replay", {
        gates, grid,
        snapshots: Number(rpForm.snapshots), step_bars: Number(rpForm.step_bars),
        hold_bars: Number(rpForm.hold_bars),
        entry_zone: Number(rpForm.entry) / 100, exit_zone: Number(rpForm.exit) / 100,
        stop_pct: Number(rpForm.stop) / 100, fee_rate: 0.0003,
      });
      loadReplay();
    } catch (e) { setRpErr((e as Error).message); }
  };

  const load = (id: string, date = "") =>
    api.get<History>(`/api/selectors/results?selector_id=${id}&date=${date}`)
      .then((h) => setHist((s) => ({ ...s, [id]: h })))
      .catch((e) => setErr((s) => ({ ...s, [id]: (e as Error).message })));

  const refresh = () =>
    api.get<Meta[]>("/api/selectors").then((ms) => {
      setMetas(ms);
      setDraft((old) => {
        const next = { ...old };
        for (const m of ms) {
          if (next[m.id]) continue;
          const d: Record<string, string> = {};
          for (const p of m.params_schema) d[p.key] = String(m.params[p.key] ?? p.default);
          next[m.id] = d;
        }
        return next;
      });
      for (const m of ms) if (!hist[m.id]) load(m.id);
    }).catch((e) => setErr((s) => ({ ...s, __page: (e as Error).message })));

  useEffect(() => {
    refresh();
    api.get<{ id: number; name: string }[]>("/api/pools").then((ps) => {
      setPools(ps); if (ps[0]) setPoolId(ps[0].id);
    }).catch(() => { /* 无自选池时下拉框为空 */ });
    loadReplay();
  }, []);

  useEffect(() => {
    if (rp?.status !== "running") return;
    const t = setInterval(loadReplay, 4000);
    return () => clearInterval(t);
  }, [rp?.status]);

  const setParam = (id: string, key: string, v: string) =>
    setDraft((d) => ({ ...d, [id]: { ...d[id], [key]: v } }));

  // 每次配置都是整份覆盖，所以把当前页面上的所有参数草稿一起提交
  const putConfig = (enabled: string[]) =>
    api.put("/api/selectors/config", { enabled, params: draft });

  const toggle = async (id: string, on: boolean) => {
    if (!metas) return;
    const enabled = metas.filter((m) => (m.id === id ? on : m.enabled)).map((m) => m.id);
    try {
      await putConfig(enabled);
      setErr((e) => ({ ...e, [id]: "" }));
      await refresh();
    } catch (e) { setErr((e2) => ({ ...e2, [id]: (e as Error).message })); }
  };

  const saveParams = async (id: string) => {
    if (!metas) return;
    try {
      await putConfig(metas.filter((m) => m.enabled).map((m) => m.id));
      setErr((e) => ({ ...e, [id]: "" }));
      await refresh();
    } catch (e) { setErr((e2) => ({ ...e2, [id]: (e as Error).message })); }
  };

  const run = async (id: string) => {
    setBusy(id); setErr((e) => ({ ...e, [id]: "" }));
    try {
      await api.post("/api/selectors/run", { selector_id: id, params: draft[id] || {} });
      await load(id);
      await refresh();
    } catch (e) { setErr((e2) => ({ ...e2, [id]: (e as Error).message })); }
    setBusy("");
  };

  const runEnabled = async () => {
    setBusy("__all"); setErr((e) => ({ ...e, __page: "" }));
    try {
      await api.post("/api/selectors/run", {});
      for (const m of metas ?? []) await load(m.id);
      await refresh();
    } catch (e) { setErr((e2) => ({ ...e2, __page: (e as Error).message })); }
    setBusy("");
  };

  const addToPool = async (id: string) => {
    const sel = checked[id];
    if (!poolId || !sel || !sel.size) return;
    try {
      await api.post(`/api/pools/${poolId}/stocks`, { codes: [...sel] });
      alert(`已把 ${sel.size} 只股票加入自选池`);
      setChecked((c) => ({ ...c, [id]: new Set() }));
    } catch (e) { setErr((e2) => ({ ...e2, [id]: (e as Error).message })); }
  };

  if (!metas) return <div className="panel">{err.__page || "加载选股器…"}</div>;

  const boxMeta = metas.find((m) => m.id === "box_range");
  const gateLabels = schemaLabels(boxMeta);
  // 只认「已完成且 job_id 对得上」的结果：运行中/失败时后端已清空旧结果，这里再挡一层
  const rpRes = rp?.status === "done" && rp.result?.job_id === rp.job_id ? rp.result : undefined;

  return (
    <>
      <div className="panel">
        <div className="row">
          <h3 style={{ margin: 0, flex: 1 }}>策略选股</h3>
          <button className="btn primary" disabled={busy !== ""} onClick={runEnabled}>
            {busy === "__all" ? "运行中…" : "运行全部启用的选股器"}
          </button>
        </div>
        <p className="muted" style={{ marginTop: 8 }}>
          每个选股器把「必须同时满足的条件」写在插件里，页面上只能调门槛数值。
          用法：① 改数值后点「保存参数」固定下来（不保存就直接「立即运行」，只影响这一次）；
          ② 点「立即运行」出名单；③ 勾选名称左边的复选框，才会被「运行全部启用的选股器」跑到。
          每天的名单会存进本地库，隔天回看历史日期不受今日行情影响。
        </p>
        <p className="up" style={{ marginTop: 6 }}>
          注意：名单会自动剔除 ST/退市风险股和当天没有新K线的股票（停牌或本地导入缺文件）。
          技术面条件依赖最新日线，先在【数据中心 → 本地数据（QMT 导入）】把当天日线导进来，
          否则这里筛不出当天新入选的股票。想固定自己的选股规则，
          在 backend/app/selectors/ 放一个 .py 并继承 Selector，刷新本页就会出现。
        </p>
        {err.__page && <div className="up">{err.__page}</div>}
      </div>

      {metas.map((m) => {
        const h = hist[m.id];
        const sel = checked[m.id] ?? new Set<string>();
        const hasBox = !!h?.rows.some((r) => r.attrs.box_top != null);
        return (
          <div className="panel" key={m.id}>
            <div className="row">
              <input type="checkbox" checked={m.enabled}
                onChange={(e) => toggle(m.id, e.target.checked)} />
              <h3 style={{ margin: 0 }}>{m.name}</h3>
              <span className="badge">{m.id}</span>
              <span style={{ flex: 1 }} />
              <span className="muted">
                {m.last?.trade_date
                  ? `最近留痕 ${m.last.trade_date}，${m.last.count} 只（${m.last.run_at.replace("T", " ").slice(5, 16)}）`
                  : "还没有留痕"}
              </span>
            </div>
            <p className="muted">{m.description}</p>

            {m.params_schema.length > 0 && (
              <div className="row" style={{ flexWrap: "wrap", rowGap: 8 }}>
                {m.params_schema.map((p) => (
                  <label className="field" key={p.key}>
                    {p.label}{p.min !== undefined && p.max !== undefined ? `（${p.min}~${p.max}）` : ""}
                    <input type="number" style={{ width: 92 }} min={p.min} max={p.max}
                      value={draft[m.id]?.[p.key] ?? ""}
                      onChange={(e) => setParam(m.id, p.key, e.target.value)} />
                  </label>
                ))}
                <button className="btn" disabled={busy !== ""} onClick={() => saveParams(m.id)}>
                  保存参数
                </button>
                <button className="btn primary" disabled={busy !== ""} onClick={() => run(m.id)}>
                  {busy === m.id ? "选股中…" : "立即运行"}
                </button>
              </div>
            )}

            {err[m.id] && <div className="up" style={{ marginTop: 8 }}>{err[m.id]}</div>}

            {h && (
              <div style={{ marginTop: 10 }}>
                <div className="row" style={{ marginBottom: 8 }}>
                  <span className="muted">
                    {h.trade_date ? `${h.trade_date} 的名单：${h.count} 只` : "这个日期还没有留痕，点「立即运行」跑一次"}
                    {h.run_at ? ` （运行于 ${h.run_at.replace("T", " ").slice(0, 16)}）` : ""}
                  </span>
                  {(h.dates ?? []).length > 0 && (
                    <select value={h.trade_date} onChange={(e) => load(m.id, e.target.value)}>
                      {(h.dates ?? []).map((d) => <option key={d} value={d}>{d}</option>)}
                    </select>
                  )}
                  <select value={poolId} onChange={(e) => setPoolId(Number(e.target.value))}>
                    {pools.map((p) => <option key={p.id} value={p.id}>加入「{p.name}」</option>)}
                  </select>
                  <button className="btn primary" onClick={() => addToPool(m.id)}
                    disabled={!sel.size}>
                    加入自选池（已选 {sel.size}）
                  </button>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th className="l">
                          <input type="checkbox"
                            checked={h.rows.length > 0 && h.rows.every((r) => sel.has(r.code))}
                            onChange={(e) => setChecked((c) => ({
                              ...c,
                              [m.id]: e.target.checked
                                ? new Set(h.rows.map((r) => r.code)) : new Set(),
                            }))} />
                        </th>
                        <th>#</th><th className="l">代码</th><th className="l">名称</th>
                        <th className="l">行业</th><th>得分</th><th className="l">入选理由</th>
                        <th>最新价</th><th>涨跌幅</th><th>流通市值(亿)</th><th>PE(TTM)</th>
                        <th>PB</th><th>ROE%</th><th>净利同比</th><th>量比</th><th>20日涨幅</th>
                        {hasBox && (<>
                          <th>箱底(元)</th><th>箱顶(元)</th><th>密集带宽%</th><th>段内振幅%</th>
                          <th>现价位置%</th><th>箱体斜率%/年</th><th>穿越次数</th><th>带内占比%</th>
                          <th>出清回撤%</th><th>箱底日期</th><th>横盘(日)</th><th>近60日挖坑</th><th>β底</th>
                        </>)}
                      </tr>
                    </thead>
                    <tbody>
                      {h.rows.map((r, i) => (
                        <tr key={r.code} onClick={() => onOpenStock(r.code)}
                          style={{ cursor: "pointer" }}>
                          <td className="l" onClick={(e) => e.stopPropagation()}>
                            <input type="checkbox" checked={sel.has(r.code)}
                              onChange={(e) => setChecked((c) => {
                                const n = new Set(c[m.id] ?? []);
                                if (e.target.checked) n.add(r.code); else n.delete(r.code);
                                return { ...c, [m.id]: n };
                              })} />
                          </td>
                          <td>{i + 1}</td>
                          <td className="l">{r.code}</td>
                          <td className="l">{r.attrs.name ?? "-"}</td>
                          <td className="l">{r.attrs.industry ?? "-"}</td>
                          <td><b>{fmt.n(r.score, 1)}</b></td>
                          <td className="l">{r.reason}</td>
                          <td>{fmt.n(r.attrs.price as number)}</td>
                          <td dangerouslySetInnerHTML={{ __html: fmt.pct(r.attrs.pct_chg as number) }} />
                          <td>{fmt.n(r.attrs.float_mv as number)}</td>
                          <td>{fmt.n(r.attrs.pe_ttm as number)}</td>
                          <td>{fmt.n(r.attrs.pb as number)}</td>
                          <td>{fmt.n(r.attrs.roe_weighted as number)}</td>
                          <td dangerouslySetInnerHTML={{
                            __html: fmt.pct(r.attrs.net_profit_yoy as number) }} />
                          <td>{fmt.n(r.attrs.vol_ratio as number)}</td>
                          <td dangerouslySetInnerHTML={{
                            __html: fmt.pct(r.attrs.chg_20d as number) }} />
                          {hasBox && (<>
                            <td>{fmt.n(r.attrs.box_bottom as number)}</td>
                            <td>{fmt.n(r.attrs.box_top as number)}</td>
                            <td>{fmt.n(r.attrs.box_height as number, 1)}</td>
                            <td>{fmt.n(r.attrs.box_ext_height as number, 1)}</td>
                            <td>{fmt.n(r.attrs.box_pos as number, 1)}</td>
                            <td>{fmt.n(r.attrs.box_slope_3y as number)}</td>
                            <td>{fmt.n(r.attrs.box_cross as number, 0)}</td>
                            <td>{fmt.n(r.attrs.box_time_pct as number, 1)}</td>
                            <td>{fmt.n(r.attrs.box_decline_pre as number, 1)}</td>
                            <td>{r.attrs.box_bottom_date ?? "-"}</td>
                            <td>{fmt.n(r.attrs.box_bars as number, 0)}</td>
                            <td>{r.attrs.box_dip_60d ? "是" : "-"}</td>
                            <td>{r.attrs.box_beta_bottom ? "β" : "-"}</td>
                          </>)}
                        </tr>
                      ))}
                      {!h.rows.length && (
                        <tr><td colSpan={16 + (hasBox ? 13 : 0)} className="l muted">这个日期没有入选的股票</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        );
      })}
      {metas.some((m) => m.id === "box_range") && (
        <div className="panel">
          <div className="row">
            <h3 style={{ margin: 0 }}>箱体回放校准</h3>
            <span className="badge">box-replay</span>
            <span style={{ flex: 1 }} />
            {rp?.status === "running" && (
              <span className="muted">{rp.progress}/{rp.total} {rp.message}</span>
            )}
          </div>
          <p className="muted" style={{ marginTop: 8 }}>
            证据回路：在过去若干个快照日上按<strong>当日的箱体口径</strong>重跑选股（不看未来），
            然后前向回放「收盘进密集带下沿 {rpForm.entry}% 内买入、到上沿 {rpForm.exit}% 内卖出、
            跌破箱底 {rpForm.stop}% 止损、持满 {rpForm.hold_bars} 个交易日超时」，
            用收益分布对照「同一只拿着不动」与「所属大盘指数」，检验门槛松紧到底值不值。
            每个快照要算一遍全市场箱体（实测 1~5 分钟），默认 6 个快照约 10~25 分钟，
            期间其它页面会排队。门槛取上面「长箱体震荡」当前的参数草稿。
          </p>
          <div className="row" style={{ flexWrap: "wrap", rowGap: 8 }}>
            {([["snapshots", "快照数"], ["step_bars", "快照间隔(交易日)"], ["hold_bars", "持有窗口(交易日)"],
               ["entry", "入场带内%(下沿)"], ["exit", "离场带内%(上沿)"], ["stop", "止损%(破箱底)"]] as const).map(([k, label]) => (
              <label className="field" key={k}>
                {label}
                <input type="number" style={{ width: 92 }} value={rpForm[k]}
                  onChange={(e) => setRpForm((f) => ({ ...f, [k]: e.target.value }))} />
              </label>
            ))}
            <label className="field">
              <input type="checkbox" checked={rpSens} onChange={(e) => setRpSens(e.target.checked)} />
              敏感性对比（振幅+50 / 回撤-20）
            </label>
            <button className="btn primary" disabled={rp?.status === "running"} onClick={startReplay}>
              {rp?.status === "running" ? "回放中…" : "启动回放校准"}
            </button>
          </div>
          {rpErr && <div className="up" style={{ marginTop: 8 }}>{rpErr}</div>}
          {rp?.status === "error" && <div className="up" style={{ marginTop: 8 }}>{rp.message}</div>}

          {rpRes?.combos?.length ? (
            <div style={{ marginTop: 12 }}>
              <div className="row" style={{ marginBottom: 8 }}>
                <span className="muted">
                  {rpRes.bases.length} 个快照 × {rpRes.combos.length} 组门槛，耗时 {fmt.n(rpRes.elapsed_sec, 0)} 秒
                  （快照日 {rpRes.bases.join("、")}）
                </span>
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th className="l">门槛</th><th>成交笔数</th><th>胜率%</th><th>收益中位%</th>
                      <th>P25%</th><th>P75%</th><th>对自身持有超额中位%</th><th>对大盘超额中位%</th>
                      <th>止损率%</th><th>超时率%</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rpRes.combos.map((c, i) => (
                      <tr key={i} style={{ cursor: "pointer", fontWeight: i === rpCombo ? 600 : 400 }}
                        onClick={() => setRpCombo(i)}>
                        <td className="l">{gatesDiff(rpRes.combos[0].gates, c.gates, gateLabels)}</td>
                        <td>{c.agg.n}</td><td>{fmt.n(c.agg.win_rate, 1)}</td>
                        <td>{fmt.n(c.agg.ret_med)}</td><td>{fmt.n(c.agg.ret_p25)}</td><td>{fmt.n(c.agg.ret_p75)}</td>
                        <td>{fmt.n(c.agg.excess_own_med)}</td><td>{fmt.n(c.agg.excess_idx_med)}</td>
                        <td>{fmt.n(c.agg.stop_rate, 1)}</td><td>{fmt.n(c.agg.timeout_rate, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {(() => {
                const c = rpRes.combos[rpCombo] ?? rpRes.combos[0];
                if (!c) return null;
                return (
                  <div style={{ marginTop: 12 }}>
                    <div className="row" style={{ marginBottom: 8 }}>
                      <span className="muted">
                        「{gatesDiff(rpRes.combos[0].gates, c.gates, gateLabels)}」逐快照与成交明细（点击上表切换门槛组合）
                      </span>
                    </div>
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>快照日</th><th>命中</th><th>成交</th><th>未入场</th><th>买前破位</th>
                          </tr>
                        </thead>
                        <tbody>
                          {c.snapshots.map((s) => (
                            <tr key={s.base}>
                              <td>{s.base}</td><td>{s.hits}</td><td>{s.trades}</td>
                              <td>{s.no_entry}</td><td>{s.broken}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <div className="table-wrap" style={{ marginTop: 8 }}>
                      <table>
                        <thead>
                          <tr>
                            <th className="l">代码</th><th className="l">名称</th><th>快照日</th>
                            <th>入场日</th><th>出场日</th><th>持有(日)</th><th>收益%</th>
                            <th>同期持有%</th><th>对大盘超额%</th><th className="l">出场原因</th>
                          </tr>
                        </thead>
                        <tbody>
                          {c.trades.map((t) => (
                            <tr key={`${t.code}-${t.base}-${t.entry_date}`}
                              onClick={() => onOpenStock(t.code)} style={{ cursor: "pointer" }}>
                              <td className="l">{t.code}</td>
                              <td className="l">{t.name}</td>
                              <td>{t.base}</td><td>{t.entry_date}</td><td>{t.exit_date}</td>
                              <td>{t.days}</td>
                              <td dangerouslySetInnerHTML={{ __html: fmt.pct(t.ret_pct) }} />
                              <td dangerouslySetInnerHTML={{ __html: fmt.pct(t.bh_pct) }} />
                              <td dangerouslySetInnerHTML={{ __html: fmt.pct(t.excess_idx_pct ?? null) }} />
                              <td className="l">{t.exit_reason}</td>
                            </tr>
                          ))}
                          {!c.trades.length && (
                            <tr><td colSpan={10} className="l muted">这组门槛在回放窗口里没有成交</td></tr>
                          )}
                        </tbody>
                      </table>
                    </div>
                  </div>
                );
              })()}
            </div>
          ) : rp?.status === "done" && (
            <p className="muted" style={{ marginTop: 8 }}>跑完了但还没有结果，再启动一次试试</p>
          )}
        </div>
      )}
    </>
  );
}
