import { fmt } from "../api";

export interface FieldDef {
  field: string; label: string; type: string;
  ops?: string[]; options?: string[];
}
export interface Cond { field: string; op: string; value: unknown }

const OP_LABEL: Record<string, string> = {
  ">=": "≥", "<=": "≤", ">": ">", "<": "<", "==": "等于", in: "属于", true: "满足",
};

export function CondRow({ cond, fields, industries, onChange, onRemove }: {
  cond: Cond;
  fields: FieldDef[];
  industries: string[];
  onChange: (c: Cond) => void;
  onRemove: () => void;
}) {
  const def = fields.find((f) => f.field === cond.field) ?? fields[0];
  const isBool = def.type === "bool";
  const ops = isBool ? ["true"] : (def.ops ?? [">=", "<="]);
  return (
    <div className="cond-row">
      <select value={def.field} onChange={(e) => {
        const nd = fields.find((f) => f.field === e.target.value)!;
        onChange({ field: nd.field, op: nd.type === "bool" ? "true" : (nd.ops?.[0] ?? ">="), value: nd.type === "bool" ? true : "" });
      }}>
        {fields.map((f) => <option key={f.field} value={f.field}>{f.label}</option>)}
      </select>
      {!isBool && (
        <select value={cond.op} onChange={(e) => onChange({ ...cond, op: e.target.value })}>
          {ops.map((o) => <option key={o} value={o}>{OP_LABEL[o] ?? o}</option>)}
        </select>
      )}
      {!isBool && def.type === "number" && (
        <input type="number" style={{ width: 110 }} value={String(cond.value ?? "")}
          onChange={(e) => onChange({ ...cond, value: e.target.value })} />
      )}
      {!isBool && def.field === "industry" && (
        <select multiple value={(cond.value as string[]) ?? []} style={{ minWidth: 160, height: 56 }}
          onChange={(e) => onChange({
            ...cond,
            op: "in",
            value: Array.from(e.target.selectedOptions).map((o) => o.value),
          })}>
          {industries.map((i) => <option key={i} value={i}>{i}</option>)}
        </select>
      )}
      {!isBool && def.type === "select" && (
        <select value={String(cond.value ?? "")}
          onChange={(e) => onChange({ ...cond, value: e.target.value })}>
          {(def.options ?? []).map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      )}
      <button className="btn danger" onClick={onRemove}>×</button>
    </div>
  );
}

export function StockResultTable({ rows, onOpenStock, selectable, checked, onCheck, onCheckAll }: {
  rows: (Record<string, unknown> & { code: string; name: string })[];
  onOpenStock: (code: string) => void;
  selectable?: boolean;
  checked?: Set<string>;
  onCheck?: (code: string, v: boolean) => void;
  onCheckAll?: (v: boolean) => void;
}) {
  const hasScore = rows.some((r) => "score" in r);
  const hasBox = rows.some((r) => r.box_top !== null && r.box_top !== undefined);
  const sigs = (r: Record<string, unknown>) => {
    const s = (r.signals ?? {}) as Record<string, boolean>;
    return Object.entries(s).filter(([, v]) => v).map(([k]) =>
      <span key={k} className="badge">{SIG_NAME[k] ?? k}</span>);
  };
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {selectable && (
              <th className="l">
                <input type="checkbox"
                  checked={!!checked && rows.length > 0 && rows.every((r) => checked.has(r.code))}
                  onChange={(e) => onCheckAll?.(e.target.checked)} />
              </th>
            )}
            <th className="l">代码</th><th className="l">名称</th><th className="l">行业</th>
            {hasScore && <th>得分</th>}
            <th>最新价</th><th>涨跌幅</th><th>成交额(亿)</th><th>换手率%</th>
            <th>市值(亿)</th><th>PE(TTM)</th><th>PB</th>
            <th>ROE</th><th>营收同比</th><th>净利同比</th><th>RSI</th>
            <th>收盘价日期</th><th className="l">信号</th>
            {hasBox && (<>
              <th>箱底(元)</th><th>箱顶(元)</th><th>箱体高度%</th><th>现价位置%</th>
              <th>三年斜率%/年</th><th>穿越次数</th><th>带内占比%</th><th>现价÷3年最低</th><th>近60日挖坑</th>
            </>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.code} onClick={() => onOpenStock(r.code)} style={{ cursor: "pointer" }}>
              {selectable && (
                <td className="l" onClick={(e) => e.stopPropagation()}>
                  <input type="checkbox" checked={checked?.has(r.code) ?? false}
                    onChange={(e) => onCheck?.(r.code, e.target.checked)} />
                </td>
              )}
              <td className="l">{r.code}</td>
              <td className="l">{r.name}</td>
              <td className="l">{(r.industry as string) ?? "-"}</td>
              {hasScore && <td><b>{fmt.n(r.score as number, 4)}</b></td>}
              <td>{fmt.n(r.price as number)}</td>
              <td dangerouslySetInnerHTML={{ __html: fmt.pct(r.pct_chg as number) }} />
              <td>{fmt.n(r.amount as number)}</td>
              <td>{fmt.n(r.turnover_rate as number)}</td>
              <td>{fmt.n(r.total_mv as number)}</td>
              <td>{fmt.n(r.pe_ttm as number)}</td>
              <td>{fmt.n(r.pb as number)}</td>
              <td>{fmt.n(r.roe_weighted as number)}</td>
              <td dangerouslySetInnerHTML={{ __html: fmt.pct(r.revenue_yoy as number) }} />
              <td dangerouslySetInnerHTML={{ __html: fmt.pct(r.net_profit_yoy as number) }} />
              <td>{fmt.n(r.rsi as number)}</td>
              <td dangerouslySetInnerHTML={{
                __html: fmt.closeDate(r.trade_date as string, r.kline_date as string) }} />
              <td className="l">{sigs(r)}</td>
              {hasBox && (<>
                <td>{fmt.n(r.box_bottom as number)}</td>
                <td>{fmt.n(r.box_top as number)}</td>
                <td>{fmt.n(r.box_height as number, 1)}</td>
                <td>{fmt.n(r.box_pos as number, 1)}</td>
                <td>{fmt.n(r.box_slope_3y as number)}</td>
                <td>{fmt.n(r.box_cross as number, 0)}</td>
                <td>{fmt.n(r.box_time_pct as number, 1)}</td>
                <td>{fmt.n(r.box_rebound as number)}</td>
                <td>{r.box_dip_60d ? "是" : "-"}</td>
              </>)}
            </tr>
          ))}
          {!rows.length && (
            <tr><td colSpan={17 + (hasScore ? 1 : 0) + (selectable ? 1 : 0) + (hasBox ? 9 : 0)}
              className="l muted">无匹配结果</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

const SIG_NAME: Record<string, string> = {
  macd_golden: "MACD金叉", macd_dead: "MACD死叉", macd_above_zero: "零轴上方",
  boll_break_up: "布林上破", boll_break_down: "布林下破", ma_bullish: "多头排列",
  ma20_up_cross: "上穿MA20", kdj_golden: "KDJ金叉", new_high_250: "年内新高",
  new_low_250: "年内新低", vol_surge: "放量",
};
