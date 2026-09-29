import { useEffect, useState } from "react";
import { api } from "../api";
import { Cond, CondRow, StockResultTable } from "./CondBuilder";
import { Row, useFields } from "./Screener";

interface Group { name: string; weight: number; conditions: Cond[] }

const defaultGroups = (): Group[] =>
  Array.from({ length: 5 }, (_, i) => ({
    name: `条件框 ${i + 1}`, weight: 20, conditions: [],
  }));

export default function AutoScreener({ onOpenStock }: { onOpenStock: (c: string) => void }) {
  const fields = useFields();
  const [groups, setGroups] = useState<Group[]>(defaultGroups);
  const [rows, setRows] = useState<(Row & { group_detail?: { group: string; weight: number; score: number }[] })[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [pools, setPools] = useState<{ id: number; name: string }[]>([]);
  const [poolId, setPoolId] = useState<number>(0);

  useEffect(() => {
    api.get<{ id: number; name: string }[]>("/api/pools").then((ps) => {
      setPools(ps); if (ps[0]) setPoolId(ps[0].id);
    });
  }, []);

  if (!fields) return <div className="panel">加载字段…</div>;

  const allFields = [
    ...fields.fundamental.map((f) => ({ ...f, label: `基本面 · ${f.label}` })),
    ...fields.technical.map((f) => ({ ...f, label: `技术面 · ${f.label}` })),
  ];
  const weightSum = groups.reduce((a, g) => a + (Number(g.weight) || 0), 0);

  const setGroup = (i: number, patch: Partial<Group>) =>
    setGroups(groups.map((g, j) => (j === i ? { ...g, ...patch } : g)));

  const run = async () => {
    setBusy(true); setErr(""); setChecked(new Set());
    try {
      const r = await api.post<{ rows: Row[] }>("/api/screen/auto", {
        groups: groups.map((g) => ({ ...g, conditions: g.conditions.filter((c) => c.field) })),
        top_n: 100,
      });
      setRows(r.rows);
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };

  const addToPool = async () => {
    if (!poolId || !checked.size) return;
    await api.post(`/api/pools/${poolId}/stocks`, { codes: [...checked] });
    alert(`已加入 ${checked.size} 只股票`);
    setChecked(new Set());
  };

  return (
    <>
      <div className="panel">
        <div className="row">
          <h3 style={{ margin: 0, flex: 1 }}>自动选股（5个条件框，权重之和必须为100）</h3>
          <span className={weightSum === 100 ? "muted" : "up"}>当前权重合计：{weightSum}</span>
        </div>
        <p className="muted" style={{ marginTop: 6 }}>
          每个框内条件按满足比例计分（0~1），总分 = Σ 框权重 × 框得分，取前100名。
        </p>
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(360px, 1fr))" }}>
          {groups.map((g, i) => (
            <div key={i} className="panel" style={{ background: "var(--panel2)" }}>
              <div className="row" style={{ marginBottom: 6 }}>
                <input style={{ flex: 1, minWidth: 100 }} value={g.name}
                  onChange={(e) => setGroup(i, { name: e.target.value })} />
                <label className="field">权重
                  <input type="number" style={{ width: 70 }} value={g.weight} min={0} max={100}
                    onChange={(e) => setGroup(i, { weight: Number(e.target.value) })} />
                </label>
              </div>
              {g.conditions.map((c, j) => (
                <CondRow key={j} cond={c} fields={allFields} industries={fields.industries}
                  onChange={(nc) => setGroup(i, { conditions: g.conditions.map((x, k) => (k === j ? nc : x)) })}
                  onRemove={() => setGroup(i, { conditions: g.conditions.filter((_, k) => k !== j) })} />
              ))}
              <button className="btn" onClick={() =>
                setGroup(i, { conditions: [...g.conditions, { field: allFields[0].field, op: ">=", value: "" }] })}>
                + 条件
              </button>
            </div>
          ))}
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button className="btn primary" disabled={busy || weightSum !== 100} onClick={run}>
            {busy ? "打分中…" : "自动选股"}
          </button>
          {weightSum !== 100 && <span className="up">权重之和必须为 100</span>}
          {err && <span className="up">{err}</span>}
        </div>
      </div>
      {rows && (
        <div className="panel">
          <div className="row" style={{ marginBottom: 10 }}>
            <h3 style={{ margin: 0, flex: 1 }}>Top {rows.length} 打分结果</h3>
            <select value={poolId} onChange={(e) => setPoolId(Number(e.target.value))}>
              {pools.map((p) => <option key={p.id} value={p.id}>加入「{p.name}」</option>)}
            </select>
            <button className="btn primary" onClick={addToPool} disabled={!checked.size}>
              加入自选池（已选 {checked.size}）
            </button>
          </div>
          <StockResultTable rows={rows} onOpenStock={onOpenStock} selectable
            checked={checked}
            onCheck={(c, v) => setChecked((s) => { const n = new Set(s); if (v) n.add(c); else n.delete(c); return n; })}
            onCheckAll={(v) => setChecked(v ? new Set(rows.map((r) => r.code)) : new Set())} />
        </div>
      )}
    </>
  );
}
