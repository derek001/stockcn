import { useEffect, useState } from "react";
import { api } from "../api";
import { Cond, CondRow, StockResultTable } from "./CondBuilder";

interface FieldsResp {
  fundamental: (import("./CondBuilder").FieldDef & { group: "f" })[];
  technical: (import("./CondBuilder").FieldDef)[];
  industries: string[];
}

export interface Row { code: string; name: string; [k: string]: unknown }

export function useFields() {
  const [fields, setFields] = useState<FieldsResp | null>(null);
  useEffect(() => {
    api.get<FieldsResp>("/api/screen/fields").then(setFields);
  }, []);
  return fields;
}

export default function Screener({ onOpenStock }: { onOpenStock: (c: string) => void }) {
  const fields = useFields();
  const [conds, setConds] = useState<Cond[]>([]);
  const [rows, setRows] = useState<Row[] | null>(null);
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

  const run = async () => {
    setBusy(true); setErr(""); setChecked(new Set());
    try {
      const r = await api.post<{ rows: Row[] }>("/api/screen", {
        conditions: conds.filter((c) => c.field),
      });
      setRows(r.rows);
    } catch (e) { setErr((e as Error).message); }
    setBusy(false);
  };

  const addToPool = async () => {
    if (!poolId || checked.size === 0) return;
    await api.post(`/api/pools/${poolId}/stocks`, { codes: [...checked] });
    alert(`已加入 ${checked.size} 只股票`);
    setChecked(new Set());
  };

  return (
    <>
      <div className="panel">
        <h3>选股条件（须同时满足全部条件）</h3>
        {conds.map((c, i) => (
          <CondRow key={i} cond={c} fields={allFields} industries={fields.industries}
            onChange={(nc) => setConds(conds.map((x, j) => (j === i ? nc : x)))}
            onRemove={() => setConds(conds.filter((_, j) => (j !== i)))} />
        ))}
        <div className="row">
          <button className="btn" onClick={() =>
            setConds([...conds, { field: allFields[0].field, op: allFields[0].ops?.[0] ?? ">=", value: "" }])}>
            + 添加条件
          </button>
          <button className="btn primary" onClick={run} disabled={busy}>
            {busy ? "筛选中…" : "开始筛选"}
          </button>
          <span className="muted">（指标基于本地K线与最新财报快照计算）</span>
        </div>
        {conds.some((c) => c.field.startsWith("box_")) && (
          <p className="muted" style={{ marginTop: 6 }}>
            条件里用到了箱体：第一次点「开始筛选」要等约 2~3 分钟（全市场把近 6 年的箱体算一遍，
            之后直到重启后端都用算好的结果，秒出）。箱底是近 6 年最低收盘价那一天，
            箱顶箱底是「80% 交易日挤在最窄价格带」的边界，不是这段行情的最高/最低价，
            所以「带内占比」恒 ≥80%，只能看不能当条件。
          </p>
        )}
        {err && <div className="up">{err}</div>}
      </div>
      {rows && (
        <div className="panel">
          <div className="row" style={{ marginBottom: 10 }}>
            <h3 style={{ margin: 0, flex: 1 }}>筛选结果：{rows.length} 只</h3>
            <select value={poolId} onChange={(e) => setPoolId(Number(e.target.value))}>
              {pools.map((p) => <option key={p.id} value={p.id}>加入「{p.name}」</option>)}
            </select>
            <button className="btn primary" onClick={addToPool}
              disabled={!checked.size}>
              加入自选池（已选 {checked.size}）
            </button>
          </div>
          <StockResultTable rows={rows} onOpenStock={onOpenStock} selectable
            checked={checked}
            onCheck={(c, v) => setChecked((s) => {
              const n = new Set(s); v ? n.add(c) : n.delete(c); return n;
            })}
            onCheckAll={(v) => setChecked(v ? new Set(rows.map((r) => r.code)) : new Set())} />
        </div>
      )}
    </>
  );
}
