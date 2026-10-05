"use client";

import { useEffect, useRef, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { fetchSnapshots } from "@/lib/api";
import { formatCompact, formatDate, formatNumber, formatSignedNumber } from "@/lib/format";
import type { ChartRange, SnapshotPoint } from "@/lib/types";

interface StarChartProps {
  owner: string;
  name: string;
  initialData: SnapshotPoint[];
  initialError?: string;
}

const RANGES: { value: ChartRange; label: string }[] = [
  { value: "30d", label: "30 天" },
  { value: "90d", label: "90 天" },
  { value: "365d", label: "1 年" },
];

export function StarChart({ owner, name, initialData, initialError }: StarChartProps) {
  const [range, setRange] = useState<ChartRange>("90d");
  const [data, setData] = useState(initialData);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(initialError ?? "");
  const requestId = useRef(0);
  const pendingRequest = useRef<AbortController | null>(null);

  useEffect(() => () => {
    requestId.current++;
    pendingRequest.current?.abort();
  }, []);

  async function selectRange(nextRange: ChartRange) {
    const currentRequest = ++requestId.current;
    pendingRequest.current?.abort();
    const controller = new AbortController();
    pendingRequest.current = controller;
    setRange(nextRange);
    setLoading(true);
    setError("");
    try {
      const payload = await fetchSnapshots(owner, name, nextRange, true, { signal: controller.signal });
      if (currentRequest === requestId.current) setData(payload.data);
    } catch (cause) {
      if (currentRequest === requestId.current && !controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "趋势数据暂时不可用，请重试。");
      }
    } finally {
      if (currentRequest === requestId.current) {
        pendingRequest.current = null;
        setLoading(false);
      }
    }
  }

  const chartData = data.map((point) => ({
    date: point.captured_at,
    label: formatDate(point.captured_at),
    stars: point.stars_count,
  }));
  const gained = chartData.length > 1
    ? chartData[chartData.length - 1].stars - chartData[0].stars
    : null;
  const canShowData = !loading && !error;
  const growthClass = !canShowData || gained === null || gained === 0
    ? "neutral" : gained < 0 ? "negative" : "positive";

  return (
    <div className={`chart-tool ${loading ? "loading" : ""}`} aria-busy={loading}>
      <div className="chart-heading">
        <div>
          <span>Star 趋势</span>
          <strong className={growthClass}>{!canShowData ? "--" : gained === null ? "历史数据不足" : formatSignedNumber(gained)}</strong>
          <small>{range === "365d" ? "1 年" : range === "30d" ? "30 天" : "90 天"}范围内已有快照的净增长</small>
        </div>
        <div className="chart-range" role="group" aria-label="图表时间范围">
          {RANGES.map((item) => (
            <button
              type="button"
              key={item.value}
              className={range === item.value ? "selected" : ""}
              aria-pressed={range === item.value}
              onClick={() => void selectRange(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>
      <div className="chart-canvas">
        {loading ? <div className="chart-state" role="status">正在加载趋势数据…</div> : null}
        {error ? <div className="chart-state" role="alert"><strong>趋势数据暂时不可用</strong><p>{error}</p><button type="button" onClick={() => void selectRange(range)}>重新加载</button></div> : null}
        {canShowData && !chartData.length ? <div className="chart-state" role="status"><strong>历史数据不足</strong><p>尚无所选范围内的快照，后续采集完成后可查看趋势。</p></div> : null}
        {canShowData && chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={chartData} margin={{ top: 14, right: 12, left: 0, bottom: 0 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 5" vertical={false} />
            <XAxis
              dataKey="label"
              axisLine={false}
              tickLine={false}
              minTickGap={50}
              tick={{ fill: "var(--faint)", fontSize: 11 }}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              width={54}
              tickFormatter={formatCompact}
              tick={{ fill: "var(--faint)", fontSize: 11 }}
              domain={["dataMin", "dataMax"]}
            />
            <Tooltip
              contentStyle={{
                color: "var(--ink)",
                border: "1px solid var(--border)",
                borderRadius: 10,
                background: "var(--surface)",
                boxShadow: "var(--shadow-sm)",
              }}
              formatter={(value) => [formatNumber(Number(value)), "Star"]}
              labelStyle={{ color: "var(--muted)", marginBottom: 6 }}
            />
            <Area
              type="monotone"
              dataKey="stars"
              stroke="var(--blue)"
              strokeWidth={2.5}
              fill="var(--blue-soft)"
              fillOpacity={0.68}
              activeDot={{ r: 4, fill: "var(--blue)", stroke: "var(--surface)", strokeWidth: 2 }}
            />
          </AreaChart>
        </ResponsiveContainer>
        ) : null}
      </div>
      <p className="chart-source">数据来源：RepoPulse 每日 GitHub 仓库快照</p>
    </div>
  );
}
