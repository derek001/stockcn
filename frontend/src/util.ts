export function ymd(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function minusYears(iso: string, years: number): string {
  const [y, m, d] = iso.split("-").map(Number);
  const t = new Date(y, m - 1, d);
  t.setFullYear(t.getFullYear() - years);
  return ymd(t);
}
