import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MarkerType,
  Position,
  type Edge,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "reactflow";
import "reactflow/dist/style.css";
import {
  Activity,
  ArrowLeftRight,
  Cpu,
  Crown,
  FileTerminal,
  GitCommitHorizontal,
  Power,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Timer,
  TriangleAlert,
  Users,
  X,
} from "lucide-react";
import { useCluster } from "../hooks/useCluster";
import { api } from "../services/api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EventList } from "../components/EventList";
import {
  DataCell,
  EmptyState,
  InlineAlert,
  PageHeader,
  RoleBadge,
  SectionHeading,
  Spinner,
  StatusDot,
} from "../components/ui";
import { classNames, formatBytes, formatDuration, formatNumber } from "../utils/format";
import type { NodeStatus } from "../types";

interface TopologyData {
  node: NodeStatus;
  leader: string | null;
  onSelect: (nodeId: string) => void;
  onCrash: (nodeId: string) => void;
  onRestart: (nodeId: string) => void;
}

interface EdgeActivity {
  lastTs: number;
  count: number;
  rpc: string;
  latency: number;
  dropped: boolean;
  direction: string;
}

const PAIR_KEY = (a: string, b: string) => [a, b].sort().join("::");

function TopologyNode({ data }: NodeProps<TopologyData>) {
  const { node, leader, onSelect, onCrash, onRestart } = data;
  const isLeader = node.node_id === leader && node.online;
  const contact = node.last_contact_age_ms;
  return (
    <div
      className={classNames(
        "w-[218px] rounded-lg border bg-console-900/95 shadow-panel transition-colors",
        !node.online
          ? "border-signal-red/30 opacity-75"
          : isLeader
            ? "border-accent-500/60 shadow-glow"
            : "border-console-600 hover:border-console-500",
      )}
      onClick={() => onSelect(node.node_id)}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter") onSelect(node.node_id);
      }}
    >
      <Handle type="target" position={Position.Top} />
      <div className="flex items-center justify-between gap-2 border-b border-console-700/60 px-3 py-2">
        <div className="flex items-center gap-2">
          {isLeader ? <Crown size={12} className="text-accent-300" /> : <Cpu size={12} className="text-console-500" />}
          <span className="font-mono text-xs font-semibold text-console-100">{node.node_id}</span>
        </div>
        <RoleBadge role={node.online ? node.role : "offline"} />
      </div>
      <div className="grid grid-cols-2 gap-x-2 gap-y-1.5 px-3 py-2 font-mono text-[10px]">
        <span className="text-console-500">term</span>
        <span className="text-right text-console-200">{node.term}</span>
        <span className="text-console-500">log</span>
        <span className="text-right text-console-200">{formatNumber(node.last_log_index)}</span>
        <span className="text-console-500">commit</span>
        <span className="text-right text-accent-300">{formatNumber(node.commit_index)}</span>
        <span className="text-console-500">applied</span>
        <span className="text-right text-console-200">{formatNumber(node.applied_index)}</span>
        <span className="text-console-500">snapshot</span>
        <span className="text-right text-console-200">{formatNumber(node.snapshot_index)}</span>
        <span className="text-console-500">{node.online ? (node.role === "leader" ? "lease" : "contact") : "pid"}</span>
        <span className={classNames("text-right", contact !== null && contact > 1500 ? "text-signal-amber" : "text-console-200")}>
          {node.online
            ? node.role === "leader"
              ? "leader"
              : contact === null
                ? "—"
                : `${formatNumber(contact)} ms`
            : (node.pid ?? "stopped")}
        </span>
      </div>
      <div className="flex items-center justify-between border-t border-console-700/60 px-2.5 py-1.5">
        <span className="font-mono text-[9px] text-console-500">
          :{node.http_port} → :{node.proxy_port}
        </span>
        {node.online ? (
          <button
            type="button"
            className="rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-signal-red/80 hover:bg-signal-red/10 hover:text-signal-red"
            onClick={(event) => {
              event.stopPropagation();
              onCrash(node.node_id);
            }}
          >
            Crash
          </button>
        ) : (
          <button
            type="button"
            className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wider text-signal-green/80 hover:bg-signal-green/10 hover:text-signal-green"
            onClick={(event) => {
              event.stopPropagation();
              onRestart(node.node_id);
            }}
          >
            <RotateCcw size={9} /> Restart
          </button>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
}

const nodeTypes = { raftNode: TopologyNode };

function layoutPositions(nodes: NodeStatus[]): Record<string, { x: number; y: number }> {
  const leader = nodes.find((node) => node.role === "leader" && node.online);
  const followers = nodes.filter((node) => node.node_id !== leader?.node_id);
  const positions: Record<string, { x: number; y: number }> = {};
  if (leader) positions[leader.node_id] = { x: 340, y: 20 };
  const startX = leader ? 60 : 20;
  followers.forEach((node, index) => {
    positions[node.node_id] = { x: startX + index * 270, y: 300 };
  });
  return positions;
}

function buildEdges(
  nodes: NodeStatus[],
  leader: string | null,
  activity: Record<string, EdgeActivity>,
  now: number,
): Edge[] {
  const edges: Edge[] = [];
  for (let left = 0; left < nodes.length; left += 1) {
    for (let right = left + 1; right < nodes.length; right += 1) {
      const a = nodes[left];
      const b = nodes[right];
      const key = PAIR_KEY(a.node_id, b.node_id);
      const info = activity[key];
      const age = info ? now - info.lastTs : Number.POSITIVE_INFINITY;
      const recentlyActive = age < 1800;
      const warm = age < 6000;
      const broken =
        !a.online || !b.online || (info !== undefined && info.dropped && age < 4000);
      const source = leader === b.node_id ? b.node_id : a.node_id;
      const target = source === a.node_id ? b.node_id : a.node_id;
      edges.push({
        id: `edge-${key}`,
        source,
        target,
        type: "straight",
        animated: recentlyActive && !broken,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          width: 12,
          height: 12,
          color: broken ? "#fb7185" : recentlyActive ? "#22d3ee" : "#3a4c5c",
        },
        style: {
          stroke: broken ? "#fb7185" : recentlyActive ? "#22d3ee" : warm ? "#22d3ee66" : "#2b3b4a",
          strokeWidth: recentlyActive ? 2 : 1.2,
          strokeDasharray: broken ? "4 4" : undefined,
          opacity: a.online && b.online ? 1 : 0.4,
        },
        label:
          info && age < 4000
            ? `${info.direction} ${info.rpc}${info.dropped ? " · dropped" : info.latency ? ` · ${formatNumber(info.latency)}ms` : ""}`
            : undefined,
        labelStyle: { fill: broken ? "#fb7185" : "#8ba0b2", fontSize: 9, fontFamily: "monospace" },
        labelBgStyle: { fill: "#0c1118", fillOpacity: 0.9 },
        labelBgPadding: [4, 2],
        labelBgBorderRadius: 3,
      });
    }
  }
  return edges;
}

export function ClusterPage() {
  const { cluster, events, loading, refresh } = useCluster();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: "crash" | "restart"; nodeId: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [activity, setActivity] = useState<Record<string, EdgeActivity>>({});
  const [now, setNow] = useState(Date.now());
  const [logs, setLogs] = useState<{ nodeId: string; content: string } | null>(null);
  const [logsBusy, setLogsBusy] = useState(false);
  const lastSeqRef = useRef<number | null>(null);
  const flowRef = useRef<ReactFlowInstance | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 900);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!events.length) return;
    const maxSeq = events[events.length - 1].seq;
    if (lastSeqRef.current === null) {
      lastSeqRef.current = maxSeq;
      return;
    }
    const fresh = events.filter(
      (event) => event.seq > (lastSeqRef.current ?? 0) && event.type === "network.message",
    );
    lastSeqRef.current = maxSeq;
    if (!fresh.length) return;
    setActivity((current) => {
      const next = { ...current };
      for (const event of fresh) {
        const src = String(event.data.src ?? "");
        const dst = String(event.data.dst ?? "");
        if (!src || !dst) continue;
        const key = PAIR_KEY(src, dst);
        const previous = next[key];
        next[key] = {
          lastTs: event.ts,
          count: (previous?.count ?? 0) + 1,
          rpc: String(event.data.rpc ?? "RPC"),
          latency: Number(event.data.latency_ms ?? 0),
          dropped: Boolean(event.data.dropped),
          direction: `${src}→${dst}`,
        };
      }
      return next;
    });
  }, [events]);

  const nodes = cluster?.nodes ?? [];
  const selected = nodes.find((node) => node.node_id === selectedId) ?? null;
  const positions = useMemo(() => layoutPositions(nodes), [nodes]);

  const graphNodes = useMemo<Node<TopologyData>[]>(
    () =>
      nodes.map((node) => ({
        id: node.node_id,
        type: "raftNode",
        position: positions[node.node_id] ?? { x: 0, y: 0 },
        width: 218,
        height: 174,
        data: {
          node,
          leader: cluster?.leader_id ?? null,
          onSelect: setSelectedId,
          onCrash: (nodeId: string) => setConfirm({ kind: "crash", nodeId }),
          onRestart: (nodeId: string) => setConfirm({ kind: "restart", nodeId }),
        },
        draggable: false,
      })),
    [nodes, positions, cluster?.leader_id],
  );

  const graphEdges = useMemo(
    () => buildEdges(nodes, cluster?.leader_id ?? null, activity, now),
    [nodes, cluster?.leader_id, activity, now],
  );

  const transportEvents = useMemo(
    () =>
      events
        .filter((event) => event.type === "network.message")
        .slice(-60)
        .reverse(),
    [events],
  );

  const runAction = useCallback(async () => {
    if (!confirm) return;
    setBusy(true);
    setActionError(null);
    try {
      if (confirm.kind === "crash") await api.crashNode(confirm.nodeId);
      else await api.restartNode(confirm.nodeId);
      await refresh();
      setConfirm(null);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Node action failed");
    } finally {
      setBusy(false);
    }
  }, [confirm, refresh]);

  const openLogs = async (nodeId: string) => {
    setLogsBusy(true);
    setActionError(null);
    try {
      const payload = await api.nodeLogs(nodeId, 160);
      setLogs({ nodeId, content: payload.log });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "Could not load node logs");
    } finally {
      setLogsBusy(false);
    }
  };

  if (!cluster) {
    return (
      <div className="panel flex min-h-[320px] items-center justify-center">
        {loading ? (
          <span className="flex items-center gap-2 text-xs text-console-400">
            <Spinner /> Loading live topology…
          </span>
        ) : (
          <EmptyState title="No cluster state" description="The controller did not return a snapshot." />
        )}
      </div>
    );
  }

  const network = cluster.simulation.network;
  const faultCount =
    network.partitions.length +
    (network.default_latency_ms > 0 ? 1 : 0) +
    (network.default_loss > 0 ? 1 : 0) +
    network.links.length +
    (cluster.simulation.random_failures.running ? 1 : 0);

  return (
    <div>
      <PageHeader
        title="Live cluster topology"
        description="Each card is one independent node process. Traffic edges animate only when the controller observes real inter-node RPC events through the fault-injection mesh."
        actions={
          <>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => flowRef.current?.fitView({ duration: 350, padding: 0.2 })}
            >
              <RefreshCw size={13} /> Fit view
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => void refresh()}>
              <Activity size={13} /> Refresh state
            </button>
          </>
        }
      />

      {actionError ? (
        <div className="mb-3">
          <InlineAlert tone="error" title="Node operation failed">
            {actionError}
          </InlineAlert>
        </div>
      ) : null}

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-5">
        <MiniStat label="Leader" value={cluster.leader_id ?? "none"} icon={<Crown size={13} />} tone={cluster.leader_id ? "cyan" : "red"} />
        <MiniStat label="Term" value={String(cluster.term)} icon={<Timer size={13} />} />
        <MiniStat
          label="Live nodes"
          value={`${cluster.active_nodes}/${cluster.total_nodes}`}
          icon={<Users size={13} />}
          tone={cluster.active_nodes >= cluster.quorum_size ? "green" : "red"}
        />
        <MiniStat label="Committed" value={formatNumber(cluster.commit_index)} icon={<GitCommitHorizontal size={13} />} />
        <MiniStat
          label="Active faults"
          value={String(faultCount + cluster.failed_nodes)}
          icon={<TriangleAlert size={13} />}
          tone={faultCount + cluster.failed_nodes > 0 ? "amber" : "green"}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="space-y-4">
          <div className="panel relative h-[500px] overflow-hidden">
            <div className="absolute left-3 top-3 z-10 flex flex-wrap items-center gap-2 rounded-md border border-console-700 bg-console-950/85 px-2 py-1 font-mono text-[9px] text-console-400 backdrop-blur">
              <span className="flex items-center gap-1"><i className="h-0.5 w-4 bg-accent-500" /> live RPC</span>
              <span className="flex items-center gap-1"><i className="h-0.5 w-4 bg-console-600" /> idle mesh</span>
              <span className="flex items-center gap-1"><i className="h-0.5 w-4 bg-signal-red" /> unavailable / dropped</span>
            </div>
            <ReactFlow
              nodes={graphNodes}
              edges={graphEdges}
              nodeTypes={nodeTypes}
              onInit={(instance) => {
                flowRef.current = instance;
              }}
              onNodeClick={(_, node) => setSelectedId(node.id)}
              fitView
              fitViewOptions={{ padding: 0.22 }}
              minZoom={0.45}
              maxZoom={1.6}
              proOptions={{ hideAttribution: true }}
              nodesConnectable={false}
              edgesFocusable={false}
            >
              <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#1b2733" />
              <Controls showInteractive={false} />
            </ReactFlow>
            <div className="pointer-events-none absolute bottom-3 left-3 z-10 font-mono text-[9px] text-console-500">
              node grid · positions are local layout; edges are sourced from network.message events
            </div>
          </div>

          <div className="panel overflow-hidden">
            <div className="panel-header">
              <SectionHeading title="Node processes" meta={`${nodes.length} independent OS processes`} />
              <span className="font-mono text-[10px] text-console-500">SIGKILL / restart supported</span>
            </div>
            <div className="overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Node</th>
                    <th>Role</th>
                    <th>Term</th>
                    <th>Log</th>
                    <th>Commit</th>
                    <th>Applied</th>
                    <th>Snapshot</th>
                    <th>KV</th>
                    <th>PID</th>
                    <th>Crashes</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {nodes.map((node) => (
                    <tr key={node.node_id} className={classNames(!node.online && "opacity-60")}>
                      <td>
                        <button
                          type="button"
                          className="flex items-center gap-2 text-left"
                          onClick={() => setSelectedId(node.node_id)}
                        >
                          <StatusDot
                            tone={!node.online ? "red" : node.node_id === cluster.leader_id ? "cyan" : "green"}
                            pulse={node.online}
                          />
                          <span className="font-mono text-xs font-medium text-console-100">{node.node_id}</span>
                        </button>
                      </td>
                      <td><RoleBadge role={node.online ? node.role : "offline"} /></td>
                      <td className="font-mono">{node.term}</td>
                      <td className="font-mono">{formatNumber(node.last_log_index)}</td>
                      <td className="font-mono text-accent-300">{formatNumber(node.commit_index)}</td>
                      <td className="font-mono">{formatNumber(node.applied_index)}</td>
                      <td className="font-mono text-console-400">{formatNumber(node.snapshot_index)}</td>
                      <td className="font-mono">{formatNumber(node.kv_count)}</td>
                      <td className="font-mono text-console-400">{node.pid ?? "—"}</td>
                      <td className="font-mono text-console-400">{node.crash_count ?? 0}</td>
                      <td>
                        <div className="flex items-center gap-1">
                          <button
                            type="button"
                            className={classNames("btn btn-xs", node.online ? "btn-danger" : "btn-ghost")}
                            disabled={!node.online}
                            onClick={() => setConfirm({ kind: "crash", nodeId: node.node_id })}
                          >
                            <Power size={10} /> Crash
                          </button>
                          <button
                            type="button"
                            className={classNames("btn btn-xs", !node.online ? "btn-primary" : "btn-ghost")}
                            disabled={node.online}
                            onClick={() => setConfirm({ kind: "restart", nodeId: node.node_id })}
                          >
                            <RotateCcw size={10} /> Restart
                          </button>
                          <button
                            type="button"
                            className="btn btn-ghost btn-xs"
                            onClick={() => void openLogs(node.node_id)}
                            disabled={logsBusy}
                          >
                            <FileTerminal size={10} /> Logs
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className="space-y-4">
          <div className="panel">
            <div className="panel-header">
              <span className="panel-title">{selected ? `${selected.node_id} details` : "Cluster details"}</span>
              {selected ? (
                <button type="button" className="text-console-500 hover:text-console-200" onClick={() => setSelectedId(null)}>
                  <X size={13} />
                </button>
              ) : null}
            </div>
            {selected ? (
              <div className="space-y-3 p-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <StatusDot tone={selected.online ? "green" : "red"} pulse={selected.online} />
                    <span className="font-mono text-sm font-semibold text-console-100">{selected.node_id}</span>
                    <RoleBadge role={selected.online ? selected.role : "offline"} />
                  </div>
                  <span className="font-mono text-[10px] text-console-500">pid {selected.pid ?? "—"}</span>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <DataCell label="Term" value={selected.term} />
                  <DataCell label="Voted for" value={selected.voted_for ?? "—"} />
                  <DataCell label="Leader" value={selected.leader_id ?? "—"} />
                  <DataCell label="Peers" value={selected.peers.length} />
                  <DataCell label="Log length" value={formatNumber(selected.log_length)} />
                  <DataCell label="Last term" value={selected.last_log_term} />
                  <DataCell label="Commit" value={formatNumber(selected.commit_index)} />
                  <DataCell label="Applied" value={formatNumber(selected.applied_index)} />
                  <DataCell label="Snapshot index" value={formatNumber(selected.snapshot_index)} />
                  <DataCell label="Snapshot term" value={selected.snapshot_term} />
                  <DataCell label="KV entries" value={formatNumber(selected.kv_count)} />
                  <DataCell label="Uptime" value={formatDuration(selected.uptime_s)} />
                  <DataCell label="DB size" value={formatBytes(selected.persistence?.db_bytes)} />
                  <DataCell label="WAL size" value={formatBytes(selected.persistence?.wal_bytes)} />
                  <DataCell label="Avg RPC" value={`${formatNumber(selected.avg_rpc_latency_ms, 1)} ms`} />
                  <DataCell label="Leader lease" value={selected.role === "leader" ? `${selected.quorum_fresh}/${selected.quorum_size} fresh` : "—"} />
                </div>
                {Object.keys(selected.next_index ?? {}).length ? (
                  <div>
                    <div className="mb-1 text-[10px] uppercase tracking-wider text-console-500">Replication progress</div>
                    <div className="space-y-1 rounded border border-console-700 bg-console-950/60 p-2 font-mono text-[10px]">
                      {Object.entries(selected.next_index).map(([peer, next]) => (
                        <div key={peer} className="flex items-center justify-between gap-2">
                          <span className="text-console-400">{peer}</span>
                          <span className="text-console-300">
                            next {next} · match {selected.match_index[peer] ?? 0}
                          </span>
                          <span className="text-console-500">{selected.last_ack_age_ms?.[peer] ?? "—"} ms</span>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
                <div className="grid grid-cols-2 gap-2">
                  <DataCell label="HTTP port" value={selected.http_port} />
                  <DataCell label="Proxy port" value={selected.proxy_port} />
                </div>
                <button type="button" className="btn btn-ghost w-full" onClick={() => void openLogs(selected.node_id)} disabled={logsBusy}>
                  <FileTerminal size={12} /> Inspect node log
                </button>
              </div>
            ) : (
              <div className="space-y-3 p-4">
                <div className="grid grid-cols-2 gap-3">
                  <DataCell label="Health" value={cluster.health} />
                  <DataCell label="Split brain" value={cluster.split_brain ? "yes" : "no"} />
                  <DataCell label="Quorum" value={`${cluster.active_nodes} / ${cluster.quorum_size}`} />
                  <DataCell label="Term" value={cluster.term} />
                  <DataCell label="Applied index" value={formatNumber(cluster.applied_index)} />
                  <DataCell label="Log entries" value={formatNumber(cluster.total_log_entries)} />
                  <DataCell label="Throughput" value={`${formatNumber(cluster.throughput, 1)} ops/s`} />
                  <DataCell label="P95 write" value={`${formatNumber(cluster.p95_latency_ms, 0)} ms`} />
                </div>
                <div className="rounded border border-console-700 bg-console-950/60 p-3 text-[11px] leading-relaxed text-console-400">
                  Select any node to inspect its durable state, replication progress and logs. Crash
                  and restart commands are sent to the process supervisor, not simulated in the UI.
                </div>
              </div>
            )}
          </div>

          <div className="panel overflow-hidden">
            <div className="panel-header">
              <span className="panel-title">Transport / network</span>
              <span className="font-mono text-[10px] text-console-500">{transportEvents.length} recent</span>
            </div>
            {faultCount > 0 ? (
              <div className="border-b border-signal-amber/20 bg-signal-amber/5 px-3 py-2 text-[10px] text-signal-amber">
                <ArrowLeftRight size={11} className="mr-1 inline" />
                {network.partitions.length ? `${network.partitions.length} partition group(s). ` : ""}
                {network.default_latency_ms > 0 ? `+${formatNumber(network.default_latency_ms)}ms global latency. ` : ""}
                {network.default_loss > 0 ? `${Math.round(network.default_loss * 100)}% loss. ` : ""}
                {network.links.length ? `${network.links.length} per-link rule(s).` : ""}
              </div>
            ) : null}
            <EventList events={transportEvents} dense maxHeight="360px" empty="No inter-node messages observed yet" />
          </div>

          {cluster.simulation.random_failures.running ? (
            <InlineAlert tone="warning" title="Random failures active">
              Seed {cluster.simulation.random_failures.config?.seed} · {cluster.simulation.random_failures.failures} failures ·{" "}
              {cluster.simulation.random_failures.recoveries} recoveries
            </InlineAlert>
          ) : null}

          <div className="panel p-4">
            <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-console-300">
              <ShieldCheck size={12} className="text-signal-green" /> Safety invariants
            </div>
            <div className="space-y-1.5 text-[11px] text-console-400">
              <SafetyLine label="Election safety" value={`≤ 1 leader / term`} />
              <SafetyLine label="Majority commit" value={`${cluster.quorum_size} of ${cluster.total_nodes} nodes required`} />
              <SafetyLine label="State machine" value={cluster.nodes.every((n) => !n.online || n.applied_index <= n.commit_index) ? "applied ≤ commit" : "index anomaly"} warn={cluster.nodes.some((n) => n.online && n.applied_index > n.commit_index)} />
              <SafetyLine label="Persistence" value="fsync before RPC reply" />
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={confirm !== null}
        title={
          confirm?.kind === "crash"
            ? `SIGKILL ${confirm.nodeId}?`
            : `Restart ${confirm?.nodeId ?? "node"}?`
        }
        description={
          confirm?.kind === "crash"
            ? "The node process is terminated immediately. If it is the leader, the remaining majority must elect a successor; clients may see transient unavailability."
            : "The node process is restarted and recovers its durable term, vote, log and state machine from SQLite. It rejoins the current leader."
        }
        confirmLabel={confirm?.kind === "crash" ? "Crash process" : "Restart process"}
        tone={confirm?.kind === "crash" ? "danger" : "primary"}
        busy={busy}
        onConfirm={() => void runAction()}
        onClose={() => !busy && setConfirm(null)}
      >
        {confirm ? (
          <div className="rounded border border-console-700 bg-console-950 p-3 font-mono text-[10px] text-console-400">
            target <span className="text-console-100">{confirm.nodeId}</span>
            {" · "}role{" "}
            <span className="text-console-100">
              {nodes.find((node) => node.node_id === confirm.nodeId)?.role ?? "offline"}
            </span>
          </div>
        ) : null}
      </ConfirmDialog>

      {logs ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-black/70" onClick={() => setLogs(null)} />
          <div className="relative flex max-h-[80vh] w-full max-w-3xl flex-col overflow-hidden rounded-lg border border-console-600 bg-console-900 shadow-2xl">
            <div className="flex items-center justify-between border-b border-console-700 px-4 py-3">
              <div className="flex items-center gap-2 text-xs font-semibold text-console-100">
                <FileTerminal size={14} className="text-accent-300" /> {logs.nodeId} process log
              </div>
              <button type="button" className="text-console-500 hover:text-console-200" onClick={() => setLogs(null)}>
                <X size={16} />
              </button>
            </div>
            <pre className="min-h-[260px] flex-1 overflow-auto whitespace-pre-wrap break-words bg-console-950 p-4 font-mono text-[10px] leading-relaxed text-console-300">
              {logs.content || "No log output captured."}
            </pre>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function MiniStat({
  label,
  value,
  icon,
  tone = "neutral",
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  tone?: "neutral" | "cyan" | "green" | "red" | "amber";
}) {
  const colors = {
    neutral: "text-console-100",
    cyan: "text-accent-300",
    green: "text-signal-green",
    red: "text-signal-red",
    amber: "text-signal-amber",
  };
  return (
    <div className="panel flex items-center gap-3 px-3 py-2.5">
      <span className="text-console-500">{icon}</span>
      <div className="min-w-0">
        <div className="text-[9px] uppercase tracking-[0.14em] text-console-500">{label}</div>
        <div className={classNames("truncate font-mono text-sm font-semibold", colors[tone])}>{value}</div>
      </div>
    </div>
  );
}

function SafetyLine({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="flex items-center gap-1.5">
        {warn ? <TriangleAlert size={10} className="text-signal-amber" /> : <ShieldCheck size={10} className="text-signal-green" />}
        {label}
      </span>
      <span className={classNames("font-mono text-[10px]", warn ? "text-signal-amber" : "text-console-300")}>{value}</span>
    </div>
  );
}
