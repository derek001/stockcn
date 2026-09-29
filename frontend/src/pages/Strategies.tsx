import { useEffect, useState } from "react";
import { api } from "../api";

interface Strategy {
  id: string; name: string; description: string; enabled: boolean;
  params_schema: { key: string; label: string; type: string; default: number }[];
}

export default function Strategies() {
  const [list, setList] = useState<Strategy[]>([]);
  const [err, setErr] = useState("");

  const load = () =>
    api.get<Strategy[]>("/api/strategies").then(setList).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []);

  const toggle = async (id: string, on: boolean) => {
    const enabled = list
      .filter((s) => (s.id === id ? on : s.enabled))
      .map((s) => s.id);
    try {
      await api.put("/api/strategies/enabled", { enabled });
      setList((ls) => ls.map((s) => (s.id === id ? { ...s, enabled: on } : s)));
    } catch (e) { setErr((e as Error).message); }
  };

  return (
    <>
      <div className="panel">
        <h3>交易策略插件</h3>
        <p className="muted" style={{ marginTop: 0 }}>
          策略以插件形式放在 backend/app/strategies/ 目录下，新增 .py 文件并继承 Strategy
          即自动被发现。勾选启用的策略会出现在【回测】页的策略下拉框中。
        </p>
        {err && <div className="up">{err}</div>}
      </div>
      {list.map((s) => (
        <div className="panel" key={s.id}>
          <div className="row">
            <input type="checkbox" checked={s.enabled} onChange={(e) => toggle(s.id, e.target.checked)} />
            <h3 style={{ margin: 0 }}>{s.name}</h3>
            <span className="badge">{s.id}</span>
          </div>
          <p className="muted">{s.description}</p>
          {s.params_schema.length > 0 && (
            <div className="row">
              参数：
              {s.params_schema.map((p) => (
                <span key={p.key} className="badge">{p.label}（默认 {p.default}）</span>
              ))}
            </div>
          )}
        </div>
      ))}
    </>
  );
}
