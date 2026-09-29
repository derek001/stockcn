const BASE = "";

async function req<T>(url: string, options?: RequestInit): Promise<T> {
  const r = await fetch(BASE + url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const j = await r.json();
      detail = j.detail ?? detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return r.json();
}

export const api = {
  get: <T,>(url: string) => req<T>(url),
  post: <T,>(url: string, body?: unknown) =>
    req<T>(url, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  put: <T,>(url: string, body: unknown) =>
    req<T>(url, { method: "PUT", body: JSON.stringify(body) }),
  del: <T,>(url: string, body?: unknown) =>
    req<T>(url, { method: "DELETE", body: body === undefined ? undefined : JSON.stringify(body) }),
};

export const fmt = {
  n(v: number | null | undefined, digits = 2): string {
    if (v === null || v === undefined || isNaN(v)) return "-";
    return v.toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
  },
  pct(v: number | null | undefined): string {
    if (v === null || v === undefined || isNaN(v)) return "-";
    const cls = v > 0 ? "up" : v < 0 ? "down" : "";
    return `<span class="${cls}">${v.toFixed(2)}%</span>`;
  },
  mv(v: number | null | undefined): string {
    if (v === null || v === undefined || isNaN(v)) return "-";
    return v >= 10000 ? (v / 10000).toFixed(2) + "万亿" : v.toFixed(1) + "亿";
  },
};
