import { useCallback, useEffect, useState } from "react";
import { api, fmt } from "../api";

interface PoolStock {
  code: string; name: string; market: string; industry: string;
  price: number; pct_chg: number; pe_ttm: number; pb: number; total_mv: number;
}
interface Pool { id: number; name: string; stocks: PoolStock[] }

export default function Pools({ onOpenStock }: { onOpenStock: (code: string) => void }) {
  const [pools, setPools] = useState<Pool[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [q, setQ] = useState("");
  const [search, setSearch] = useState<{ code: string; name: string }[]>([]);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    api.get<Pool[]>("/api/pools").then((ps) => {
      setPools(ps);
      setActiveId((cur) => (cur && ps.some((p) => p.id === cur) ? cur : ps[0]?.id ?? null));
    }).catch((e) => setErr(String(e.message)));
  }, []);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!q) { setSearch([]); return; }
    const t = setTimeout(() =>
      api.get<{ code: string; name: string }[]>(`/api/stocks?q=${encodeURIComponent(q)}&limit=20`)
        .then(setSearch).catch(() => []), 300);
    return () => clearTimeout(t);
  }, [q]);

  const active = pools.find((p) => p.id === activeId);

  const createPool = async () => {
    const name = prompt("股票池名称", "新股票池");
    if (!name) return;
    await api.post("/api/pools", { name });
    load();
  };
  const renamePool = async (p: Pool) => {
    const name = prompt("修改名称", p.name);
    if (!name || name === p.name) return;
    await api.put(`/api/pools/${p.id}`, { name });
    load();
  };
  const deletePool = async (p: Pool) => {
    if (!confirm(`确认删除股票池「${p.name}」？`)) return;
    await api.del(`/api/pools/${p.id}`);
    load();
  };
  const addStock = async (code: string, name: string) => {
    if (!active) return;
    await api.post(`/api/pools/${active.id}/stocks`, { codes: [code] });
    setQ(""); setSearch([]);
    load();
    void name;
  };
  const removeStock = async (code: string) => {
    if (!active) return;
    await api.del(`/api/pools/${active.id}/stocks`, { codes: [code] });
    load();
  };

  return (
    <div className="pool-list">
      <div className="pool-side">
        <div className="panel">
          <h3>股票池</h3>
          <button className="btn primary" onClick={createPool}>+ 新建股票池</button>
        </div>
        {pools.map((p) => (
          <div key={p.id}
            className={`item ${p.id === activeId ? "active" : ""}`}
            onClick={() => setActiveId(p.id)}>
            <span>{p.name} <span className="muted">({p.stocks.length})</span></span>
            <span>
              <button className="btn" style={{ padding: "2px 6px" }}
                onClick={(e) => { e.stopPropagation(); renamePool(p); }}>改名</button>{" "}
              <button className="btn danger" style={{ padding: "2px 6px" }}
                onClick={(e) => { e.stopPropagation(); deletePool(p); }}>删</button>
            </span>
          </div>
        ))}
      </div>

      <div style={{ flex: 1 }}>
        {err && <div className="panel up">{err}</div>}
        <div className="panel">
          <h3>添加股票到「{active?.name ?? "-"}」</h3>
          <div style={{ position: "relative" }}>
            <input style={{ width: 320 }} placeholder="输入代码或名称搜索，如 600519 / 茅台"
              value={q} onChange={(e) => setQ(e.target.value)} />
            {search.length > 0 && (
              <div className="panel" style={{
                position: "absolute", top: 36, left: 0, width: 320, margin: 0,
                padding: 6, background: "var(--panel2)", zIndex: 20,
              }}>
                {search.map((s) => (
                  <div key={s.code} className="stock-card" style={{ marginBottom: 4 }}
                    onClick={() => addStock(s.code, s.name)}>
                    {s.code} {s.name}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="panel">
          <h3>{active?.name} 成分股</h3>
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th className="l">代码</th><th className="l">名称</th><th className="l">行业</th>
                <th>最新价</th><th>涨跌幅</th><th>PE(TTM)</th><th>总市值</th><th className="l">操作</th>
              </tr></thead>
              <tbody>
                {(active?.stocks ?? []).map((s) => (
                  <tr key={s.code} style={{ cursor: "pointer" }} onClick={() => onOpenStock(s.code)}>
                    <td className="l">{s.code}</td>
                    <td className="l">{s.name}</td>
                    <td className="l">{s.industry ?? "-"}</td>
                    <td>{fmt.n(s.price)}</td>
                    <td dangerouslySetInnerHTML={{ __html: fmt.pct(s.pct_chg) }} />
                    <td>{fmt.n(s.pe_ttm)}</td>
                    <td>{s.total_mv ? fmt.mv(s.total_mv / 1e8) : "-"}</td>
                    <td className="l">
                      <button className="btn danger" style={{ padding: "2px 8px" }}
                        onClick={(e) => { e.stopPropagation(); removeStock(s.code); }}>移除</button>
                    </td>
                  </tr>
                ))}
                {(active?.stocks ?? []).length === 0 && (
                  <tr><td colSpan={8} className="muted l">股票池为空，请通过搜索或选股功能添加</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
