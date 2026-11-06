import { useState } from "react";
import { ChevronRight } from "lucide-react";
import type { ClusterEvent } from "../types";
import { classNames, formatDateTime, formatTime } from "../utils/format";
import { SeverityBadge } from "./ui";

const SOURCE_TONE: Record<string, string> = {
  node: "text-accent-300",
  network: "text-signal-violet",
  controller: "text-console-300",
  client: "text-signal-green",
};

export function EventRow({ event, dense = false }: { event: ClusterEvent; dense?: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const hasData = Object.keys(event.data ?? {}).length > 0;
  const isCritical = event.severity === "error" || event.severity === "critical";
  return (
    <div
      className={classNames(
        "group border-b border-console-800/60 last:border-0",
        isCritical && "bg-signal-red/[0.03]",
      )}
    >
      <button
        type="button"
        className={classNames(
          "flex w-full items-start gap-2.5 text-left transition-colors hover:bg-console-850/60",
          dense ? "px-3 py-1.5" : "px-3 py-2",
        )}
        onClick={() => hasData && setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className="mt-0.5 shrink-0 font-mono text-[10px] tabular-nums text-console-500">
          {formatTime(event.ts)}
        </span>
        <span className="mt-0.5 shrink-0">
          <SeverityBadge severity={event.severity} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
            <span className="font-mono text-[10px] text-console-500">{event.type}</span>
            {event.node_id ? (
              <span className="font-mono text-[10px] text-console-400">[{event.node_id}]</span>
            ) : null}
            <span
              className={classNames(
                "text-[9px] uppercase tracking-wider",
                SOURCE_TONE[event.source] ?? "text-console-500",
              )}
            >
              {event.source}
            </span>
          </span>
          <span className="mt-0.5 block truncate text-[11px] leading-relaxed text-console-200">
            {event.message}
          </span>
        </span>
        {hasData ? (
          <ChevronRight
            size={12}
            className={classNames(
              "mt-1 shrink-0 text-console-600 transition-transform",
              expanded && "rotate-90",
            )}
          />
        ) : null}
      </button>
      {expanded ? (
        <div className="border-t border-console-800/60 bg-console-950/50 px-3 py-2">
          <div className="mb-1 text-[10px] uppercase tracking-wider text-console-500">
            {formatDateTime(event.ts)} · seq {event.seq} · {event.type}
          </div>
          <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded border border-console-800 bg-console-950 p-2 font-mono text-[10px] leading-relaxed text-console-300">
            {JSON.stringify(event.data, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

export function EventList({
  events,
  dense = false,
  empty = "No events yet",
  maxHeight,
}: {
  events: ClusterEvent[];
  dense?: boolean;
  empty?: string;
  maxHeight?: string;
}) {
  if (!events.length) {
    return <div className="px-3 py-8 text-center text-xs text-console-500">{empty}</div>;
  }
  return (
    <div className="overflow-y-auto" style={maxHeight ? { maxHeight } : undefined}>
      {events.map((event) => (
        <EventRow key={event.seq} event={event} dense={dense} />
      ))}
    </div>
  );
}
