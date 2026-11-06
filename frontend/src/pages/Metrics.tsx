import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  Award,
  BarChart3,
  Clock3,
  Cpu,
  ExternalLink,
  Gauge,
  GitCommitHorizontal,
  LineChart as LineChartIcon,
  Play,
  RefreshCw,
  Server,
  ShieldCheck,
  Timer,
  TriangleAlert,
  Zap,
} from "lucide-react";
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
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
import { ConfirmDialog } from "../components/ConfirmDialog";
import { chartAxisProps, chartGridProps, DarkTooltip } from "../components/charts";
import { EmptyState, InlineAlert, PageHeader, RoleBadge, SectionHeading, Spinner, StatusDot } from "../components/ui";
import { classNames, formatDuration, formatMs, formatNumber, formatTime } from "../utils/format";
import type { ExperimentReport, MetricsPoint } from "../types";

export function MetricsPage() {
  const { metrics, cluster, refresh } = useCluster();
  const [benchmarkOpen, setBenchmarkOpen] = useState(false);
  const [benchmarkBusy, setBenchmarkBusy] = useState(false);
  const [benchmarkError, setBenchmarkError] = useState<string | null>(null);
  const [benchmark, setBenchmark] = useState<ExperimentReport | null>(null);
  const [duration, setDuration] = useState(5);
  const [rate, setRate] = useState(6);

  const history = metrics?.history ?? [];
  const current = useMemo<MetricsPoint | null>(
    () => (metrics && Object.keys(metrics.current).length ? (metrics.current as MetricsPoint) : null),
    [metrics],
  );
  const averages = useMemo(() => {
    const last = history.slice(-60);
    if (!last.length) return { throughput: 0, latency: 0, availability: 0, replication: 0 };
    return {
      throughput: last.reduce((sum, point) => sum + point.throughput, 0) / last.length,
      latency: last.reduce((sum, point) => sum + point.write_latency_p95_ms, 0) / last.length,
      availability: last.reduce((sum, point) => sum + point.node_availability_pct, 0) / last.length,
      replication: last.reduce((sum, point) => sum + point.replication_latency_ms, 0) / last.length,
    };
  }, [history]);

  const runBenchmark = async () => {
    setBenchmarkBusy(true);
    setBenchmarkError(null);
    try {
      const report = await api.runBenchmark(duration, rate);
      setBenchmark(report);
      setBenchmarkOpen(false);
      await refresh();
    } catch (error) {
      setBenchmarkError(error instanceof Error ? error.message : "Benchmark failed");
    } finally {
      setBenchmarkBusy(false);
    }
  };

  const scenarios = benchmark?.scenarios ?? [];
  const totalSuccess = metrics?.totals.successful_ops ?? 0;
  const totalFailed = metrics?.totals.failed_ops ?? 0;

  return (
    <div>
      <PageHeader
        title="Metrics & benchmarks"
        description="One-second samples from the controller, computed from live commit indexes and client-observed latencies. The benchmark executes the full six-scenario suite against real node processes."
        actions={
          <>
            <a className="btn btn-ghost" href="/api/prometheus" target="_blank" rel="noreferrer">
              <ExternalLink size={13} /> Prometheus
            </a>
            <button type="button" className="btn btn-primary" onClick={() => setBenchmarkOpen(true)}>
              <Play size={13} /> Run benchmark suite
            </button>
          </>
        }
      />

      {benchmarkError ? <div className="mb-3"><InlineAlert tone="error" title="Benchmark failed">{benchmarkError}</InlineAlert></div> : null}
      {benchmark ? (
        <div className="mb-4">
          <InlineAlert
            tone={benchmark.outcome === "passed" ? "success" : "warning"}
            title={`Benchmark ${benchmark.outcome}`}
            action={<Link className="btn btn-ghost btn-xs" to="/reports">open report</Link>}
          >
            {scenarios.length} scenarios · {formatNumber(benchmark.summary?.throughput_ops_s ?? 0, 1)} ops/s · p95{" "}
            {formatMs(benchmark.summary?.p95_ms)} · consistency {benchmark.summary?.consistency}
          </InlineAlert>
        </div>
      ) : null}

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4 xl:grid-cols-6">
        <MetricStat label="Commit rate" value={`${formatNumber(current?.throughput ?? averages.throughput, 1)}`} unit="entries/s" detail={`leader commit-index growth · 60s avg ${formatNumber(averages.throughput, 1)}`} icon={<Zap size={13} />} tone="cyan" />
        <MetricStat label="P95 latency" value={formatMs(current?.write_latency_p95_ms ?? averages.latency, 0)} detail={`avg ${formatMs(current?.write_latency_avg_ms ?? 0, 0)}`} icon={<Timer size={13} />} tone={(current?.write_latency_p95_ms ?? 0) > 250 ? "amber" : "green"} />
        <MetricStat label="Availability" value={`${formatNumber(current?.node_availability_pct ?? averages.availability, 1)}%`} detail={`${cluster?.active_nodes ?? 0}/${cluster?.total_nodes ?? 0} nodes`} icon={<Server size={13} />} tone={(current?.node_availability_pct ?? 0) >= 100 ? "green" : "amber"} />
        <MetricStat label="Successful writes" value={formatNumber(totalSuccess)} detail={`${formatNumber(totalFailed)} failed`} icon={<Award size={13} />} />
        <MetricStat label="Commit index" value={formatNumber(current?.commit_index ?? cluster?.commit_index)} detail={`term ${current?.term ?? cluster?.term ?? 0}`} icon={<GitCommitHorizontal size={13} />} />
        <MetricStat label="Replication latency" value={formatMs(current?.replication_latency_ms ?? averages.replication, 1)} detail={`network ${formatMs(current?.network_latency_ms ?? 0, 1)}`} icon={<Activity size={13} />} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <ChartPanel title="Commit rate" subtitle="leader commit-index growth / second (includes no-op entries)" icon={<Zap size={13} />} meta="1s samples">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={history.slice(-120)}>
                <CartesianGrid {...chartGridProps} />
                <XAxis dataKey="ts" tickFormatter={formatTime} {...chartAxisProps} minTickGap={30} />
                <YAxis {...chartAxisProps} width={36} />
                <Tooltip content={<DarkTooltip suffix=" entries/s" />} />
                <Area type="monotone" dataKey="throughput" name="commit rate" stroke="#22d3ee" fill="#22d3ee" fillOpacity={0.1} strokeWidth={1.5} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="Collecting commit-rate samples" />
          )}
        </ChartPanel>

        <ChartPanel title="Write latency" subtitle="client round-trip percentiles" icon={<Clock3 size={13} />} meta="p50 / p95 / p99">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history.slice(-120)}>
                <CartesianGrid {...chartGridProps} />
                <XAxis dataKey="ts" tickFormatter={formatTime} {...chartAxisProps} minTickGap={30} />
                <YAxis {...chartAxisProps} width={38} unit="ms" />
                <Tooltip content={<DarkTooltip suffix=" ms" />} />
                <Line type="monotone" dataKey="write_latency_p50_ms" name="p50" stroke="#34d399" dot={false} strokeWidth={1.3} isAnimationActive={false} />
                <Line type="monotone" dataKey="write_latency_p95_ms" name="p95" stroke="#fbbf24" dot={false} strokeWidth={1.4} isAnimationActive={false} />
                <Line type="monotone" dataKey="write_latency_p99_ms" name="p99" stroke="#fb7185" dot={false} strokeWidth={1.4} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="No write samples yet" />
          )}
        </ChartPanel>

        <ChartPanel title="Node availability" subtitle="online nodes as a percentage" icon={<Server size={13} />} meta="all nodes">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={history.slice(-120)}>
                <CartesianGrid {...chartGridProps} />
                <XAxis dataKey="ts" tickFormatter={formatTime} {...chartAxisProps} minTickGap={30} />
                <YAxis {...chartAxisProps} width={38} domain={[0, 100]} unit="%" />
                <Tooltip content={<DarkTooltip suffix="%" />} />
                <Area type="stepAfter" dataKey="node_availability_pct" name="availability" stroke="#34d399" fill="#34d399" fillOpacity={0.08} strokeWidth={1.4} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="No availability samples" />
          )}
        </ChartPanel>

        <ChartPanel title="Replication & network" subtitle="commit replication vs. simulated wire latency" icon={<LineChartIcon size={13} />} meta="milliseconds">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={history.slice(-120)}>
                <CartesianGrid {...chartGridProps} />
                <XAxis dataKey="ts" tickFormatter={formatTime} {...chartAxisProps} minTickGap={30} />
                <YAxis {...chartAxisProps} width={42} unit="ms" />
                <Tooltip content={<DarkTooltip suffix=" ms" />} />
                <Line type="monotone" dataKey="replication_latency_ms" name="replication" stroke="#a78bfa" dot={false} strokeWidth={1.4} isAnimationActive={false} />
                <Line type="monotone" dataKey="network_latency_ms" name="network" stroke="#60a5fa" dot={false} strokeWidth={1.4} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="No transport samples" />
          )}
        </ChartPanel>

        <ChartPanel title="Election duration" subtitle="from candidate start to leader elected" icon={<Gauge size={13} />} meta="observed elections">
          {(metrics?.election_durations_ms.length ?? 0) ? (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={(metrics?.election_durations_ms ?? []).slice(-20).map((value, index) => ({ election: index + 1, ms: value }))}>
                <CartesianGrid {...chartGridProps} />
                <XAxis dataKey="election" {...chartAxisProps} />
                <YAxis {...chartAxisProps} width={42} unit="ms" />
                <Tooltip content={<DarkTooltip suffix=" ms" />} />
                <Bar dataKey="ms" name="election" fill="#22d3ee" fillOpacity={0.65} radius={[2, 2, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="No elections observed" description="Election durations appear after the first real leader change." icon={<ShieldCheck size={20} />} />
          )}
        </ChartPanel>

        <ChartPanel title="Operation outcomes" subtitle="successful vs. failed client operations" icon={<BarChart3 size={13} />} meta="per one-second sample">
          {history.length ? (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={history.slice(-60)}>
                <CartesianGrid {...chartGridProps} />
                <XAxis dataKey="ts" tickFormatter={formatTime} {...chartAxisProps} minTickGap={30} />
                <YAxis {...chartAxisProps} width={32} />
                <Tooltip content={<DarkTooltip />} />
                <Bar dataKey="successful_ops" name="success" fill="#34d399" stackId="ops" isAnimationActive={false} />
                <Bar dataKey="failed_ops" name="failed" fill="#fb7185" stackId="ops" isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyState title="No client operations sampled" />
          )}
        </ChartPanel>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="panel overflow-hidden">
          <div className="panel-header">
            <SectionHeading title="Node counters" meta="live process observability" />
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => void refresh()}>
              <RefreshCw size={10} /> refresh
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Node</th>
                  <th>Role</th>
                  <th>Term</th>
                  <th>Commit</th>
                  <th>Applied</th>
                  <th>KV</th>
                  <th>RPC avg</th>
                  <th>Recent commits</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {(cluster?.nodes ?? []).map((node) => (
                  <tr key={node.node_id} className={classNames(!node.online && "opacity-60")}>
                    <td>
                      <span className="flex items-center gap-2">
                        <StatusDot tone={node.online ? (node.role === "leader" ? "cyan" : "green") : "red"} pulse={node.online} />
                        <span className="font-mono text-xs text-console-100">{node.node_id}</span>
                      </span>
                    </td>
                    <td><RoleBadge role={node.online ? node.role : "offline"} /></td>
                    <td className="font-mono">{node.term}</td>
                    <td className="font-mono text-accent-300">{formatNumber(node.commit_index)}</td>
                    <td className="font-mono">{formatNumber(node.applied_index)}</td>
                    <td className="font-mono">{formatNumber(node.kv_count)}</td>
                    <td className="font-mono">{formatMs(node.avg_rpc_latency_ms, 1)}</td>
                    <td>
                      <div className="flex h-4 items-end gap-0.5">
                        {(node.recent_commit_latency_ms ?? []).slice(-14).map((value, index) => (
                          <span
                            key={index}
                            className="w-1 rounded-sm bg-accent-500/60"
                            style={{ height: `${Math.max(2, Math.min(16, value / 8))}px` }}
                            title={`${value} ms`}
                          />
                        ))}
                      </div>
                    </td>
                    <td>
                      <span className={classNames("font-mono text-[10px]", node.online ? "text-signal-green" : "text-signal-red")}>
                        {node.online ? "online" : "down"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-console-700/60 px-4 py-2 font-mono text-[10px] text-console-500">
            <span>uptime {formatDuration(cluster?.uptime_s)} · {history.length} samples in window</span>
            <span>{formatNumber(metrics?.election_durations_ms.length ?? 0)} elections recorded</span>
          </div>
        </div>

        <div className="space-y-4">
          <div className="panel p-4">
            <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-console-300">
              <Cpu size={12} /> Prometheus scrape
            </div>
            <p className="text-[11px] leading-relaxed text-console-400">
              Custom gauges and counters are exported at <code className="rounded bg-console-950 px-1 py-0.5 font-mono text-accent-300">/metrics</code>.
              No metric is synthetic: values are derived from node status, commit indexes, and recorded client latency.
            </p>
            <a href="/api/prometheus" target="_blank" rel="noreferrer" className="btn btn-ghost mt-3 w-full">
              <ExternalLink size={12} /> Open scrape endpoint
            </a>
          </div>

          <div className="panel p-4">
            <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-console-300">
              <ShieldCheck size={12} className="text-signal-green" /> Methodology
            </div>
            <ul className="space-y-1.5 text-[11px] leading-relaxed text-console-400">
              <li>• Throughput = Δ leader commit index / wall-clock time.</li>
              <li>• Client latency is measured around the full HTTP commit round trip.</li>
              <li>• Availability counts online node processes, not inferred UI state.</li>
              <li>• Election duration is emitted by the winning candidate.</li>
              <li>• Failed operations are real rejected or timed-out requests.</li>
            </ul>
          </div>

          <div className="rounded-lg border border-signal-amber/20 bg-signal-amber/[0.04] p-3 text-[10px] leading-relaxed text-signal-amber/90">
            <TriangleAlert size={11} className="mr-1 inline" />
            Benchmarks run six fault scenarios in sequence. Each scenario injects real failures,
            so clients may observe temporary write failures. Results are not smoothed.
          </div>
        </div>
      </div>

      {scenarios.length ? (
        <div className="panel mt-4 overflow-hidden">
          <div className="panel-header">
            <SectionHeading title="Latest benchmark breakdown" meta={benchmark?.id} />
            <Link to="/reports" className="text-[10px] text-accent-300 hover:text-accent-400">full report →</Link>
          </div>
          <div className="overflow-x-auto">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Scenario</th>
                  <th>Outcome</th>
                  <th>Throughput</th>
                  <th>P50</th>
                  <th>P95</th>
                  <th>P99</th>
                  <th>Write failures</th>
                  <th>Recovery</th>
                  <th>Consistency</th>
                </tr>
              </thead>
              <tbody>
                {scenarios.map((scenario) => (
                  <tr key={scenario.scenario}>
                    <td className="font-mono">{scenario.scenario}</td>
                    <td><span className={classNames("chip", scenario.outcome === "passed" ? "chip-green" : "chip-red")}>{scenario.outcome}</span></td>
                    <td className="font-mono">{formatNumber(scenario.throughput_ops_s, 2)}/s</td>
                    <td className="font-mono">{formatMs(scenario.p50_ms)}</td>
                    <td className="font-mono">{formatMs(scenario.p95_ms)}</td>
                    <td className="font-mono">{formatMs(scenario.p99_ms)}</td>
                    <td className="font-mono">{formatNumber(scenario.write_failures)}</td>
                    <td className="font-mono">{scenario.recovery_s === null ? "—" : `${formatNumber(scenario.recovery_s, 2)} s`}</td>
                    <td><span className={classNames("font-mono text-[10px]", scenario.consistency ? "text-signal-green" : "text-signal-red")}>{scenario.consistency ? "passed" : "failed"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}

      <ConfirmDialog
        open={benchmarkOpen}
        title="Run full benchmark suite?"
        description="Six scenarios will run sequentially against the live cluster: normal, follower down, leader crash, latency, packet loss and partition. The cluster will be faulted and recovered repeatedly."
        confirmLabel="Run six scenarios"
        tone="danger"
        busy={benchmarkBusy}
        onConfirm={() => void runBenchmark()}
        onClose={() => !benchmarkBusy && setBenchmarkOpen(false)}
      >
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="benchmark-duration">Seconds per scenario</label>
              <input
                id="benchmark-duration"
                type="number"
                min={3}
                max={60}
                className="input"
                value={duration}
                onChange={(event) => setDuration(Math.max(3, Math.min(60, Number(event.target.value) || 3)))}
              />
            </div>
            <div>
              <label className="label" htmlFor="benchmark-rate">Target rate (ops/s)</label>
              <input
                id="benchmark-rate"
                type="number"
                min={0.5}
                max={100}
                step={0.5}
                className="input"
                value={rate}
                onChange={(event) => setRate(Math.max(0.5, Math.min(100, Number(event.target.value) || 6)))}
              />
            </div>
          </div>
          <div className="rounded border border-console-700 bg-console-950 p-3 text-[10px] leading-relaxed text-console-400">
            Estimated minimum wall time: ~{Math.ceil(duration * 6 + 30)} s. The request stays open
            until all scenarios and final convergence checks complete.
            {benchmarkBusy ? <span className="mt-1 flex items-center gap-2 text-accent-300"><Spinner /> running…</span> : null}
          </div>
        </div>
      </ConfirmDialog>
    </div>
  );
}

function MetricStat({
  label,
  value,
  unit,
  detail,
  icon,
  tone = "neutral",
}: {
  label: string;
  value: string;
  unit?: string;
  detail: string;
  icon: React.ReactNode;
  tone?: "neutral" | "green" | "red" | "amber" | "cyan";
}) {
  const colors = {
    neutral: "text-console-100",
    green: "text-signal-green",
    red: "text-signal-red",
    amber: "text-signal-amber",
    cyan: "text-accent-300",
  };
  return (
    <div className="panel px-3 py-3">
      <div className="flex items-center justify-between">
        <span className="text-[9px] uppercase tracking-[0.13em] text-console-500">{label}</span>
        <span className="text-console-600">{icon}</span>
      </div>
      <div className={classNames("mt-1 font-mono text-xl font-semibold", colors[tone])}>
        {value}
        {unit ? <span className="ml-1 text-[10px] font-normal text-console-500">{unit}</span> : null}
      </div>
      <div className="mt-0.5 truncate text-[10px] text-console-500">{detail}</div>
    </div>
  );
}

function ChartPanel({
  title,
  subtitle,
  meta,
  icon,
  children,
}: {
  title: string;
  subtitle: string;
  meta: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="panel">
      <div className="panel-header">
        <div className="flex items-center gap-2">
          <span className="text-console-500">{icon}</span>
          <div>
            <div className="panel-title">{title}</div>
            <div className="mt-0.5 text-[10px] text-console-500">{subtitle}</div>
          </div>
        </div>
        <span className="font-mono text-[10px] text-console-500">{meta}</span>
      </div>
      <div className="h-[210px] px-2 py-3">{children}</div>
    </div>
  );
}
