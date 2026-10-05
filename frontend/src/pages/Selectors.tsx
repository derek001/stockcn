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

export default function Selectors({ onOpenStock }: { onOpenStock: (c: string) => void }) {
  const [metas, setMetas] = useState<Meta[] | null>(null);
  const [draft, setDraft] = useState<Record<string, Record<string, string>>>({});
  const [hist, setHist] = useState<Record<string, History>>({});
  const [checked, setChecked] = useState<Record<string, Set<string>>>({});
  const [pools, setPools] = useState<{ id: number; name: string }[]>([]);
  const [poolId, setPoolId] = useState(0);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState<Record<string, string>>({});

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
  }, []);

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
                          <th>出清回撤%</th><th>箱底日期</th><th>横盘(日)</th><th>近60日挖坑</th>
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
                          </>)}
                        </tr>
                      ))}
                      {!h.rows.length && (
                        <tr><td colSpan={16 + (hasBox ? 12 : 0)} className="l muted">这个日期没有入选的股票</td></tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}
