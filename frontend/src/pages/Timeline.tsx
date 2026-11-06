import { useMemo, useState } from "react";
import {
  Activity,
  Download,
  Filter,
  ListTree,
  Pause,
  Play,
  Search,
  Trash2,
  X,
} from "lucide-react";
import { useCluster } from "../hooks/useCluster";
import { EventList } from "../components/EventList";
import { EmptyState, PageHeader, SectionHeading, StatusDot } from "../components/ui";
import {
  classNames,
  downloadJson,
  formatDateTime,
  formatNumber,
  severityRank,
} from "../utils/format";
import type { ClusterEvent, Severity } from "../types";

const SEVERITIES: Severity[] = ["debug", "info", "warning", "error", "critical"];

export function TimelinePage() {
  const { events, cluster } = useCluster();
  const [paused, setPaused] = useState(false);
  const [frozen, setFrozen] = useState<ClusterEvent[]>([]);
  const [source, setSource] = useState("all");
  const [node, setNode] = useState("all");
  const [minSeverity, setMinSeverity] = useState<Severity | "all">("info");
  const [search, setSearch] = useState("");
  const [hideHeartbeats, setHideHeartbeats] = useState(true);

  const pool = paused ? frozen : events;
  const nodeIds = useMemo(() => {
    const ids = new Set<string>();
    for (const event of events) if (event.node_id) ids.add(event.node_id);
    for (const item of cluster?.nodes ?? []) ids.add(item.node_id);
    return Array.from(ids).sort();
  }, [events, cluster]);

  const sources = useMemo(() => {
    const values = new Set<string>(["node", "network", "controller", "client"]);
    for (const event of events) values.add(event.source);
    return Array.from(values).sort();
  }, [events]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const threshold = minSeverity === "all" ? -1 : severityRank(minSeverity);
    return pool
      .filter((event) => {
        if (source !== "all" && event.source !== source) return false;
        if (node !== "all" && event.node_id !== node) return false;
        if (minSeverity !== "all" && severityRank(event.severity) < threshold) return false;
        if (hideHeartbeats && (event.type === "heartbeat_ack" || (event.type === "network.message" && event.data.rpc === "Heartbeat"))) return false;
        if (needle) {
          const haystack = `${event.type} ${event.message} ${event.node_id ?? ""} ${JSON.stringify(event.data)}`.toLowerCase();
          if (!haystack.includes(needle)) return false;
        }
        return true;
      })
      .slice()
      .reverse();
  }, [pool, source, node, minSeverity, hideHeartbeats, search]);

  const counts = useMemo(() => {
    const bySeverity: Record<string, number> = {};
    const bySource: Record<string, number> = {};
    for (const event of filtered) {
      bySeverity[event.severity] = (bySeverity[event.severity] ?? 0) + 1;
      bySource[event.source] = (bySource[event.source] ?? 0) + 1;
    }
    return { bySeverity, bySource };
  }, [filtered]);

  const errors = (counts.bySeverity.error ?? 0) + (counts.bySeverity.critical ?? 0);
  const warnings = counts.bySeverity.warning ?? 0;
  const lastEvent = filtered[0];

  const togglePause = () => {
    if (!paused) {
      setFrozen(events);
      setPaused(true);
    } else {
      setPaused(false);
      setFrozen([]);
    }
  };

  const clearFilters = () => {
    setSource("all");
    setNode("all");
    setMinSeverity("all");
    setSearch("");
    setHideHeartbeats(true);
  };

  const filtersActive =
    source !== "all" || node !== "all" || minSeverity !== "all" || search.trim() !== "" || !hideHeartbeats;

  return (
    <div>
      <PageHeader
        title="Event timeline"
        description="Structured events emitted by real node elections, RPC transport, process supervision, client commits and experiment control. Expand an event to inspect its raw payload."
        actions={
          <>
            <button type="button" className={classNames("btn", paused ? "btn-primary" : "btn-ghost")} onClick={togglePause}>
              {paused ? <Play size={13} /> : <Pause size={13} />}
              {paused ? `Resume (${events.length - frozen.length} buffered)` : "Pause stream"}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => downloadJson(`raft-events-${Date.now()}.json`, { exported_at: new Date().toISOString(), events: filtered })}
              disabled={!filtered.length}
            >
              <Download size={13} /> Export view
            </button>
          </>
        }
      />

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <TimelineStat label="Visible events" value={formatNumber(filtered.length)} detail={`${formatNumber(pool.length)} in buffer`} tone="cyan" />
        <TimelineStat label="Errors" value={formatNumber(errors)} detail="error + critical" tone={errors ? "red" : "green"} />
        <TimelineStat label="Warnings" value={formatNumber(warnings)} detail="dropped / degraded" tone={warnings ? "amber" : "green"} />
        <TimelineStat
          label="Last event"
          value={lastEvent ? lastEvent.severity : "—"}
          detail={lastEvent ? formatDateTime(lastEvent.ts) : "no matching events"}
          tone="neutral"
        />
      </div>

      <div className="panel mb-4">
        <div className="panel-header">
          <div className="flex items-center gap-2">
            <Filter size={13} className="text-console-500" />
            <SectionHeading title="Filters" meta={`${formatNumber(events.length)} live events buffered`} />
          </div>
          <div className="flex items-center gap-2">
            {paused ? (
              <span className="chip chip-amber"><Pause size={10} /> paused snapshot</span>
            ) : (
              <span className="chip chip-green"><StatusDot tone="green" pulse /> streaming</span>
            )}
            {filtersActive ? (
              <button type="button" className="btn btn-ghost btn-xs" onClick={clearFilters}>
                <X size={10} /> Clear
              </button>
            ) : null}
          </div>
        </div>
        <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label className="label" htmlFor="event-source">Source</label>
            <select id="event-source" className="select" value={source} onChange={(event) => setSource(event.target.value)}>
              <option value="all">All sources</option>
              {sources.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="event-node">Node</label>
            <select id="event-node" className="select" value={node} onChange={(event) => setNode(event.target.value)}>
              <option value="all">All nodes</option>
              {nodeIds.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="event-severity">Minimum severity</label>
            <select
              id="event-severity"
              className="select"
              value={minSeverity}
              onChange={(event) => setMinSeverity(event.target.value as Severity | "all")}
            >
              <option value="all">Any severity</option>
              {SEVERITIES.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </div>
          <div>
            <label className="label" htmlFor="event-search">Search</label>
            <div className="relative">
              <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-console-500" />
              <input
                id="event-search"
                className="input py-2 pl-7 text-xs"
                placeholder="type, message or payload…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-4 border-t border-console-700/60 px-4 py-2.5 text-[10px] text-console-400">
          <label className="flex items-center gap-1.5">
            <input type="checkbox" checked={hideHeartbeats} onChange={(event) => setHideHeartbeats(event.target.checked)} />
            Hide heartbeat RPCs
          </label>
          <div className="flex flex-wrap gap-2">
            {Object.entries(counts.bySource).sort(([a], [b]) => a.localeCompare(b)).map(([name, count]) => (
              <span key={name} className="rounded border border-console-700 px-1.5 py-0.5 font-mono">
                {name} {count}
              </span>
            ))}
          </div>
          <button
            type="button"
            className="ml-auto flex items-center gap-1 text-console-500 hover:text-console-300"
            onClick={() => setSearch("")}
            disabled={!search}
          >
            <Trash2 size={10} /> clear search
          </button>
        </div>
      </div>

      <div className="panel overflow-hidden">
        <div className="panel-header">
          <div className="flex items-center gap-2">
            <ListTree size={13} className="text-console-500" />
            <span className="panel-title">Events</span>
            <span className="font-mono text-[10px] text-console-500">newest first · click to expand</span>
          </div>
          <span className="font-mono text-[10px] text-console-500">
            {filtered.length ? `seq ${formatNumber(filtered[0].seq)}` : "no events"}
          </span>
        </div>
        {filtered.length ? (
          <EventList events={filtered} maxHeight="min(64vh, 760px)" />
        ) : (
          <EmptyState
            icon={<ListTree size={22} />}
            title={events.length ? "No events match these filters" : "Waiting for events"}
            description={events.length ? "Adjust or clear the filters above." : "The controller and nodes emit events as they run."}
            action={filtersActive ? <button type="button" className="btn btn-ghost btn-xs" onClick={clearFilters}>Clear filters</button> : undefined}
          />
        )}
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-console-700/60 px-4 py-2 font-mono text-[10px] text-console-500">
          <span>{paused ? "snapshot frozen at pause time" : "live WebSocket stream + REST backfill"}</span>
          <span className="flex items-center gap-1"><Activity size={10} /> errors {errors} · warnings {warnings}</span>
        </div>
      </div>
    </div>
  );
}

function TimelineStat({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail: string;
  tone: "neutral" | "cyan" | "green" | "red" | "amber";
}) {
  const colors = {
    neutral: "text-console-100",
    cyan: "text-accent-300",
    green: "text-signal-green",
    red: "text-signal-red",
    amber: "text-signal-amber",
  };
  return (
    <div className="panel px-3 py-3">
      <div className="text-[9px] uppercase tracking-[0.14em] text-console-500">{label}</div>
      <div className={classNames("mt-1 truncate font-mono text-lg font-semibold uppercase", colors[tone])}>{value}</div>
      <div className="mt-0.5 truncate text-[10px] text-console-500">{detail}</div>
    </div>
  );
}
