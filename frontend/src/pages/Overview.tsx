import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowRight,
  Crown,
  Database,
  FlaskConical,
  Gauge,
  GitCommitHorizontal,
  Network,
  Play,
  ShieldAlert,
  ShieldCheck,
  Timer,
  TriangleAlert,
  Users,
  Zap,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useCluster } from "../hooks/useCluster";
import { api } from "../services/api";
import {
  classNames,
  formatDuration,
  formatMs,
  formatNumber,
  formatTime,
} from "../utils/format";
import { EventList } from "../components/EventList";
import { chartAxisProps, chartGridProps, DarkTooltip } from "../components/charts";
import {
  EmptyState,
  HealthBadge,
  InlineAlert,
  PageHeader,
  RoleBadge,
  Spinner,
  StatCard,
  StatusDot,
} from "../components/ui";
import type { NodeStatus } from "../types";

function healthMessage(health: string, leader: string | null): string {
  if (health === "healthy") return "A leader is elected and every node is reachable.";
  if (health === "degraded") return "The cluster has quorum, but at least one node is unavailable.";
  if (health === "critical") {
    return leader
      ? "The cluster is below quorum or reports conflicting leaders."
      : "No leader is currently elected. Client writes cannot commit.";
  }
  return "The cluster controller is stopped.";
}

export function OverviewPage() {
  const { cluster, metrics, events, loading, refresh } = useCluster();
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const history = metrics?.history ?? [];
  const recentEvents = useMemo(
    () =>
      events
        .filter((event) => event.severity !== "debug")
        .filter((event) => !(event.type === "network.message" && event.data?.rpc === "Heartbeat"))
        .slice(-9)
        .reverse(),
    [events],
  );

  const startCluster = async () => {
    setStarting(true);
    setError(null);
    try {
      await api.startCluster();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start cluster");
    } finally {
      setStarting(false);
    }
  };

  if (!cluster) {
    return (
      <div className="panel flex min-h-[320px] items-center justify-center">
        {loading ? (
          <div className="flex items-center gap-2 text-xs text-console-400">
            <Spinner /> Connecting to controller…
          </div>
        ) : (
          <EmptyState
            title="Controller unavailable"
            description="The dashboard could not load cluster state. Check that the backend is running on port 8000."
          />
        )}
      </div>
    );
  }

  const faults: string[] = [];
  const network = cluster.simulation.network;
  if (network.partitions.length) faults.push("Network partition");
  if (network.default_latency_ms > 0) faults.push(`${network.default_latency_ms} ms latency`);
  if (network.default_loss > 0) faults.push(`${Math.round(network.default_loss * 100)}% packet loss`);
  if (cluster.simulation.random_failures.running) faults.push("Random failure experiment");
  if (cluster.failed_nodes > 0) faults.push(`${cluster.failed_nodes} node(s) down`);

  return (
    <div>
      <PageHeader
        title="Cluster overview"
        description="Live Raft state sourced from every node process. Writes are acknowledged only after majority commit and local apply."
        actions={
          <>
            <Link to="/simulator" className="btn btn-ghost">
              <FlaskConical size={13} /> Simulate failure
            </Link>
            <Link to="/kv" className="btn btn-primary">
              <Database size={13} /> Open key-value
            </Link>
          </>
        }
      />

      {error ? (
        <div className="mb-4">
          <InlineAlert tone="error" title="Command failed">
            {error}
          </InlineAlert>
        </div>
      ) : null}

      <div
        className={classNames(
          "mb-5 flex flex-col gap-3 rounded-lg border px-4 py-3 sm:flex-row sm:items-center sm:justify-between",
          cluster.health === "healthy"
            ? "border-signal-green/20 bg-signal-green/[0.04]"
            : cluster.health === "degraded"
              ? "border-signal-amber/25 bg-signal-amber/[0.04]"
              : "border-signal-red/25 bg-signal-red/[0.04]",
        )}
      >
        <div className="flex items-start gap-3">
          <div
            className={classNames(
              "mt-0.5",
              cluster.health === "healthy"
                ? "text-signal-green"
                : cluster.health === "degraded"
                  ? "text-signal-amber"
                  : "text-signal-red",
            )}
          >
            {cluster.health === "healthy" ? <ShieldCheck size={18} /> : <ShieldAlert size={18} />}
          </div>
          <div>
            <div className="flex flex-wrap items-center gap-2 text-sm font-medium text-console-100">
              {cluster.health === "stopped" ? "Cluster stopped" : healthMessage(cluster.health, cluster.leader_id)}
              <HealthBadge health={cluster.health} />
              {cluster.split_brain ? (
                <span className="chip chip-red">
                  <TriangleAlert size={10} /> split brain
                </span>
              ) : null}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[10px] text-console-400">
              <span>
                leader <span className="text-console-200">{cluster.leader_id ?? "none"}</span>
              </span>
              <span>
                quorum{" "}
                <span className="text-console-200">
                  {cluster.active_nodes}/{cluster.total_nodes}
                </span>{" "}
                (need {cluster.quorum_size})
              </span>
              <span>
                commit <span className="text-console-200">{formatNumber(cluster.commit_index)}</span>
              </span>
              <span>uptime {formatDuration(cluster.uptime_s)}</span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {cluster.status === "stopped" ? (
            <button type="button" className="btn btn-primary" onClick={startCluster} disabled={starting}>
              {starting ? <Spinner /> : <Play size={13} />} Start cluster
            </button>
          ) : (
            <Link to="/reports" className="btn btn-ghost text-[11px]">
              Run experiment <ArrowRight size={12} />
            </Link>
          )}
        </div>
      </div>

      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
        <StatCard
          label="Leader"
          value={cluster.leader_id ?? "none"}
          detail={cluster.leader_id ? `term ${cluster.term}` : "election pending"}
          icon={<Crown size={14} />}
          tone={cluster.leader_id ? "cyan" : "red"}
        />
        <StatCard
          label="Raft term"
          value={cluster.term}
          detail={`election ${cluster.election_ms !== null ? formatMs(cluster.election_ms) : "—"}`}
          icon={<Gauge size={14} />}
        />
        <StatCard
          label="Nodes online"
          value={`${cluster.active_nodes}/${cluster.total_nodes}`}
          detail={`${cluster.failed_nodes} failed · quorum ${cluster.quorum_size}`}
          icon={<Users size={14} />}
          tone={cluster.active_nodes >= cluster.quorum_size ? "green" : "red"}
          spark={history.slice(-30).map((point) => point.active_nodes)}
        />
        <StatCard
          label="Commit index"
          value={formatNumber(cluster.commit_index)}
          detail={`${formatNumber(cluster.total_log_entries)} log entries across nodes`}
          icon={<GitCommitHorizontal size={14} />}
          spark={history.slice(-30).map((point) => point.commit_index)}
        />
        <StatCard
          label="Throughput"
          value={formatNumber(cluster.throughput, 1)}
          detail="committed entries / sec"
          icon={<Zap size={14} />}
          tone="cyan"
          spark={history.slice(-30).map((point) => point.throughput)}
        />
        <StatCard
          label="P95 write latency"
          value={formatMs(cluster.p95_latency_ms, 1)}
          detail={`avg ${formatMs(cluster.avg_latency_ms, 1)}`}
          icon={<Timer size={14} />}
          tone={cluster.p95_latency_ms > 250 ? "amber" : "neutral"}
          spark={history.slice(-30).map((point) => point.write_latency_p95_ms)}
        />
      </div>

      {faults.length ? (
        <div className="mb-5 flex flex-wrap items-center gap-2 rounded-lg border border-signal-amber/20 bg-signal-amber/[0.04] px-3 py-2 text-[11px] text-signal-amber">
          <TriangleAlert size={13} />
          <span className="font-semibold uppercase tracking-wider">Active faults</span>
          {faults.map((fault) => (
            <span key={fault} className="rounded-full border border-signal-amber/25 px-2 py-0.5">
              {fault}
            </span>
          ))}
          <Link to="/simulator" className="ml-auto text-signal-amber/80 hover:text-signal-amber">
            Inspect simulator →
          </Link>
        </div>
      ) : null}

      <div className="mb-5 grid gap-4 xl:grid-cols-2">
        <div className="panel">
          <div className="panel-header">
            <div className="flex items-center gap-2">
              <Activity size={13} className="text-console-500" />
              <span className="panel-title">Commit throughput</span>
            </div>
            <span className="font-mono text-[10px] text-console-500">1s samples · last 60s</span>
          </div>
          <div className="h-[190px] px-2 py-3">
            {history.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={history.slice(-60)}>
                  <CartesianGrid {...chartGridProps} />
                  <XAxis dataKey="ts" tickFormatter={formatTime} {...chartAxisProps} minTickGap={28} />
                  <YAxis {...chartAxisProps} width={34} />
                  <Tooltip content={<DarkTooltip suffix=" ops/s" />} />
                  <Area
                    type="monotone"
                    dataKey="throughput"
                    name="throughput"
                    stroke="#22d3ee"
                    strokeWidth={1.5}
                    fill="#22d3ee"
                    fillOpacity={0.08}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState title="Collecting metrics" description="Charts appear after the first samples." />
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <div className="flex items-center gap-2">
              <Timer size={13} className="text-console-500" />
              <span className="panel-title">Write latency</span>
            </div>
            <span className="font-mono text-[10px] text-console-500">p50 / p95 / p99</span>
          </div>
          <div className="h-[190px] px-2 py-3">
            {history.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={history.slice(-60)}>
                  <CartesianGrid {...chartGridProps} />
                  <XAxis dataKey="ts" tickFormatter={formatTime} {...chartAxisProps} minTickGap={28} />
                  <YAxis {...chartAxisProps} width={38} unit="ms" />
                  <Tooltip content={<DarkTooltip suffix=" ms" />} />
                  <Line
                    type="monotone"
                    dataKey="write_latency_p50_ms"
                    name="p50"
                    stroke="#34d399"
                    dot={false}
                    strokeWidth={1.4}
                    isAnimationActive={false}
                  />
                  <Line
                    type="monotone"
                    dataKey="write_latency_p95_ms"
                    name="p95"
                    stroke="#fbbf24"
                    dot={false}
                    strokeWidth={1.4}
                    isAnimationActive={false}
                  />
                  <Line
                    type="monotone"
                    dataKey="write_latency_p99_ms"
                    name="p99"
                    stroke="#fb7185"
                    dot={false}
                    strokeWidth={1.4}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState title="No write samples" />
            )}
          </div>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.55fr_1fr]">
        <div className="panel overflow-hidden">
          <div className="panel-header">
            <div className="flex items-center gap-2">
              <Network size={13} className="text-console-500" />
              <span className="panel-title">Node state</span>
            </div>
            <Link to="/cluster" className="text-[10px] text-accent-300 hover:text-accent-400">
              open topology →
            </Link>
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
                  <th>Contact</th>
                  <th>Quorum</th>
                </tr>
              </thead>
              <tbody>
                {cluster.nodes.map((node) => (
                  <NodeRow key={node.node_id} node={node} leader={cluster.leader_id} />
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel overflow-hidden">
          <div className="panel-header">
            <span className="panel-title">Recent events</span>
            <Link to="/timeline" className="text-[10px] text-accent-300 hover:text-accent-400">
              full timeline →
            </Link>
          </div>
          <EventList events={recentEvents} dense empty="Waiting for cluster events" />
        </div>
      </div>
    </div>
  );
}

function NodeRow({ node, leader }: { node: NodeStatus; leader: string | null }) {
  const contact =
    node.role === "leader"
      ? "leader"
      : node.online
        ? node.last_contact_age_ms < 1000
          ? `${node.last_contact_age_ms} ms`
          : formatMs(node.last_contact_age_ms)
        : "offline";
  return (
    <tr className={classNames(!node.online && "opacity-60")}>
      <td>
        <div className="flex items-center gap-2">
          <StatusDot
            tone={!node.online ? "red" : node.node_id === leader ? "cyan" : "green"}
            pulse={node.online}
          />
          <span className="font-mono text-xs font-medium text-console-100">{node.node_id}</span>
        </div>
      </td>
      <td>
        <RoleBadge role={node.role} />
      </td>
      <td className="font-mono">{node.term}</td>
      <td className="font-mono">{node.last_log_index}</td>
      <td className="font-mono text-accent-300">{node.commit_index}</td>
      <td className="font-mono">{node.applied_index}</td>
      <td className="font-mono text-console-400">{node.snapshot_index}</td>
      <td className="font-mono text-console-400">{contact}</td>
      <td>
        {node.role === "leader" ? (
          <span
            className={classNames(
              "font-mono text-[10px]",
              node.quorum_fresh >= node.quorum_size ? "text-signal-green" : "text-signal-amber",
            )}
          >
            {node.quorum_fresh}/{node.quorum_size} fresh
          </span>
        ) : (
          <span className="font-mono text-[10px] text-console-500">
            {node.online ? `election in ${formatMs(node.election_timeout_remaining_ms)}` : "—"}
          </span>
        )}
      </td>
    </tr>
  );
}
