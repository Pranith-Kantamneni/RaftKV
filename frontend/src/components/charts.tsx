import { formatTime } from "../utils/format";

export const chartGridProps = {
  stroke: "#1b2733",
  strokeDasharray: "3 3",
  vertical: false,
} as const;

export const chartAxisProps = {
  stroke: "#3a4c5c",
  tick: { fill: "#5c7285", fontSize: 10 },
  tickLine: false,
  axisLine: { stroke: "#1b2733" },
} as const;

interface TooltipItem {
  name?: string;
  value?: number | string;
  color?: string;
  dataKey?: string;
  unit?: string;
}

export function DarkTooltip({
  active,
  payload,
  label,
  suffix = "",
}: {
  active?: boolean;
  payload?: TooltipItem[];
  label?: number | string;
  suffix?: string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-console-600 bg-console-900/95 px-2.5 py-2 shadow-xl">
      {typeof label === "number" ? (
        <div className="mb-1 font-mono text-[10px] text-console-400">{formatTime(label)}</div>
      ) : null}
      {payload.map((item) => (
        <div key={`${item.dataKey}`} className="flex items-center gap-2 text-[11px]">
          <span
            className="h-1.5 w-1.5 rounded-full"
            style={{ backgroundColor: item.color ?? "#22d3ee" }}
          />
          <span className="text-console-400">{item.name}</span>
          <span className="ml-auto font-mono text-console-100">
            {typeof item.value === "number" ? item.value.toFixed(2) : item.value}
            {suffix}
          </span>
        </div>
      ))}
    </div>
  );
}
