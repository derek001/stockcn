import { useEffect, useRef } from "react";
import * as echarts from "echarts";

export interface Bar {
  date: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
  ma5?: number | null;
  ma10?: number | null;
  ma20?: number | null;
  ma60?: number | null;
  dif?: number | null;
  dea?: number | null;
  macd?: number | null;
  boll_up?: number | null;
  boll_mid?: number | null;
  boll_low?: number | null;
}

interface Props {
  bars: Bar[];
  height?: number;
  showMA?: boolean;
  showBOLL?: boolean;
  showMACD?: boolean;
  showVol?: boolean;
  lineMode?: boolean;
  markers?: { date: string; type: "buy" | "sell"; price: number }[];
  title?: string;
}

export default function KLineChart({
  bars, height = 520, showMA = true, showBOLL = false, showMACD = false,
  showVol = true, lineMode = false, markers = [], title,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current, "dark");
    const dates = bars.map((b) => b.date);
    const series: echarts.SeriesOption[] = [];
    const legends: string[] = [];

    if (lineMode) {
      series.push({
        name: title ?? "", type: "line", data: bars.map((b) => b.close ?? null),
        showSymbol: false, lineStyle: { width: 1.5, color: "#4f8cff" },
      });
      if (title) legends.push(title);
    } else {
      series.push({
        name: "K线", type: "candlestick",
        data: bars.map((b) => [b.open, b.close, b.low, b.high]),
        itemStyle: {
          color: "#ef5350", color0: "#26a69a",
          borderColor: "#ef5350", borderColor0: "#26a69a",
        },
        markPoint: markers.length ? {
          symbolSize: 42,
          data: markers.map((m) => ({
            name: m.type,
            coord: [m.date, m.price],
            value: m.type === "buy" ? "B" : "S",
            itemStyle: { color: m.type === "buy" ? "#e64545" : "#18a058" },
            label: { color: "#fff", fontSize: 10, formatter: m.type === "buy" ? "B" : "S" },
          })),
        } : undefined,
      });
      if (showMA) {
        const colors: Record<string, string> = {
          ma5: "#f7b500", ma10: "#e15bef", ma20: "#4f8cff", ma60: "#5be499",
        };
        for (const k of ["ma5", "ma10", "ma20", "ma60"] as const) {
          if (bars.some((b) => b[k] != null)) {
            series.push({
              name: k.toUpperCase(), type: "line", data: bars.map((b) => b[k] ?? null),
              showSymbol: false, lineStyle: { width: 1, color: colors[k] },
            });
            legends.push(k.toUpperCase());
          }
        }
      }
      if (showBOLL) {
        for (const [k, n] of [["boll_up", "BOLL上轨"], ["boll_mid", "中轨"], ["boll_low", "下轨"]] as const) {
          if (bars.some((b) => (b as Bar)[k] != null)) {
            series.push({
              name: n, type: "line", data: bars.map((b) => (b as Bar)[k] ?? null),
              showSymbol: false, lineStyle: { width: 1, type: "dashed", color: "#8a94a6" },
            });
            legends.push(n);
          }
        }
      }
    }

    const grids: object[] = [];
    const xAxes: object[] = [];
    const yAxes: object[] = [];
    let topPct = 4;
    const volH = 12, macdH = 15;
    const mainH = showVol
      ? (showMACD ? 46 : 58)
      : (showMACD ? 62 : 84);
    grids.push({ left: 60, right: 20, top: `${topPct}%`, height: `${mainH}%` });
    xAxes.push({ type: "category", data: dates, gridIndex: 0, axisLabel: { show: !showVol && !showMACD } });
    yAxes.push({ scale: true, gridIndex: 0 });
    if (showVol) {
      topPct += mainH + 3;
      grids.push({ left: 60, right: 20, top: `${topPct}%`, height: `${volH}%` });
      xAxes.push({ type: "category", data: dates, gridIndex: 1, axisLabel: { show: false } });
      yAxes.push({ gridIndex: 1, axisLabel: { formatter: (v: number) => v / 1e6 + "M" } });
      series.push({
        name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
        data: bars.map((b) => b.volume ?? 0),
        itemStyle: { color: "#3a4b6b" },
      });
    }
    if (showMACD) {
      topPct = showVol ? (4 + mainH + 3 + volH + 3) : (4 + mainH + 3);
      grids.push({ left: 60, right: 20, top: `${topPct}%`, height: `${macdH}%` });
      xAxes.push({ type: "category", data: dates, gridIndex: grids.length - 1 });
      yAxes.push({ gridIndex: grids.length - 1 });
      series.push({
        name: "MACD", type: "bar", xAxisIndex: grids.length - 1, yAxisIndex: grids.length - 1,
        data: bars.map((b) => ({
          value: b.macd ?? 0,
          itemStyle: { color: (b.macd ?? 0) >= 0 ? "#ef5350" : "#26a69a" },
        })),
      });
      series.push({
        name: "DIF", type: "line", xAxisIndex: grids.length - 1, yAxisIndex: grids.length - 1,
        data: bars.map((b) => b.dif ?? null), showSymbol: false, lineStyle: { width: 1, color: "#f7b500" },
      });
      series.push({
        name: "DEA", type: "line", xAxisIndex: grids.length - 1, yAxisIndex: grids.length - 1,
        data: bars.map((b) => b.dea ?? null), showSymbol: false, lineStyle: { width: 1, color: "#4f8cff" },
      });
    }

    if (xAxes.length) {
      (xAxes[xAxes.length - 1] as { axisLabel?: unknown }).axisLabel = { show: true };
    }

    chart.setOption({
      backgroundColor: "transparent",
      animation: false,
      legend: { data: legends, top: 0, textStyle: { color: "#8a94a6" } },
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: grids,
      xAxis: xAxes,
      yAxis: yAxes,
      dataZoom: [{ type: "inside", xAxisIndex: xAxes.map((_, i) => i) }],
      series,
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(ref.current);
    return () => {
      window.removeEventListener("resize", onResize);
      ro.disconnect();
      chart.dispose();
    };
  }, [bars, markers, showMA, showBOLL, showMACD, showVol, lineMode, title, height]);

  return <div ref={ref} className="chart" style={{ height }} />;
}
