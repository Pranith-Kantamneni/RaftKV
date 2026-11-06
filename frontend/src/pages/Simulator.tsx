import { useMemo, useState } from "react";
import {
  Activity,
  AlertOctagon,
  ArrowLeftRight,
  Ban,
  CheckCircle2,
  Clock3,
  Dices,
  Gauge,
  Layers,
  PauseCircle,
  Play,
  Power,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Skull,
  SlidersHorizontal,
  TriangleAlert,
  Users,
  WifiOff,
  Zap,
} from "lucide-react";
import { useCluster } from "../hooks/useCluster";
import { api } from "../services/api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  EmptyState,
  InlineAlert,
  PageHeader,
  RoleBadge,
  SectionHeading,
  Spinner,
  StatusDot,
} from "../components/ui";
import { classNames, formatDuration, formatMs, formatNumber } from "../utils/format";

type GroupId = 1 | 2 | 3;

interface PendingAction {
  title: string;
  description: string;
  confirmLabel: string;
  tone: "danger" | "primary";
  run: () => Promise<void>;
  detail?: React.ReactNode;
}

const LATENCY_PRESETS = [100, 500, 1000, 3000];
const LOSS_PRESETS = [0, 10, 25, 50, 75];

function asNumber(value: string, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function SimulatorPage() {
  const { cluster, refresh, loading } = useCluster();
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [groups, setGroups] = useState<Record<string, GroupId>>({});
  const [latency, setLatency] = useState(0);
  const [latencyTarget, setLatencyTarget] = useState("");
  const [loss, setLoss] = useState(0);
  const [lossTarget, setLossTarget] = useState("");

  const [probability, setProbability] = useState(0.35);
  const [intervalS, setIntervalS] = useState(4);
  const [recoveryDelayS, setRecoveryDelayS] = useState(4);
  const [maxFailures, setMaxFailures] = useState(2);
  const [seed, setSeed] = useState(1234);
  const [durationS, setDurationS] = useState(30);
  const [useDuration, setUseDuration] = useState(false);

  const nodes = cluster?.nodes ?? [];
  const randomState = cluster?.simulation.random_failures;
  const network = cluster?.simulation.network;
  const leader = cluster?.leader_id ?? null;

  const groupAssignments = useMemo(() => {
    const result: Record<string, GroupId> = {};
    nodes.forEach((node, index) => {
      result[node.node_id] = groups[node.node_id] ?? (((index % 2) + 1) as GroupId);
    });
    return result;
  }, [nodes, groups]);

  const partitionGroups = useMemo(() => {
    const buckets: Record<GroupId, string[]> = { 1: [], 2: [], 3: [] };
    for (const [nodeId, group] of Object.entries(groupAssignments)) buckets[group].push(nodeId);
    return buckets;
  }, [groupAssignments]);

  const activeFaults = useMemo(() => {
    if (!cluster || !network) return [];
    const faults: string[] = [];
    if (network.partitions.length) faults.push(`Partition: ${network.partitions.map((group) => group.join("+")).join(" | ")}`);
    if (network.default_latency_ms > 0) faults.push(`Global latency ${formatNumber(network.default_latency_ms)} ms`);
    if (network.default_loss > 0) faults.push(`Global loss ${Math.round(network.default_loss * 100)}%`);
    for (const link of network.links) {
      faults.push(
        `${link.source}→${link.target}${link.latency_ms ? ` +${formatNumber(link.latency_ms)}ms` : ""}${link.loss ? ` ${Math.round(link.loss * 100)}% loss` : ""}`,
      );
    }
    if (randomState?.running) faults.push(`Random failures (seed ${randomState.config?.seed})`);
    if (cluster.failed_nodes) faults.push(`${cluster.failed_nodes} crashed node(s)`);
    return faults;
  }, [cluster, network, randomState]);

  const execute = async (label: string, action: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await action();
      await refresh();
      setMessage(label);
      setPending(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Simulation command failed");
    } finally {
      setBusy(false);
    }
  };

  const confirmAction = (action: Omit<PendingAction, "run"> & { run: () => Promise<void> }) => {
    setPending(action);
  };

  if (!cluster) {
    return (
      <div className="panel flex min-h-[320px] items-center justify-center">
        {loading ? (
          <span className="flex items-center gap-2 text-xs text-console-400">
            <Spinner /> Connecting to controller…
          </span>
        ) : (
          <EmptyState title="Simulator unavailable" description="No cluster snapshot was returned by the controller." />
        )}
      </div>
    );
  }

  const applyPartition = () => {
    const groupsToApply = Object.values(partitionGroups).filter((group) => group.length > 0);
    if (groupsToApply.length < 2) {
      setError("Assign nodes to at least two groups before applying a partition.");
      return;
    }
    confirmAction({
      title: "Apply network partition?",
      description: "Inter-node RPCs between different groups will be dropped immediately. A group without a majority cannot elect a leader or commit writes.",
      confirmLabel: "Partition network",
      tone: "danger",
      detail: (
        <div className="space-y-1 rounded border border-console-700 bg-console-950 p-3 font-mono text-[10px]">
          {groupsToApply.map((group, index) => (
            <div key={index} className="flex items-center justify-between gap-3">
              <span className="text-console-500">group {index + 1}</span>
              <span className="text-console-100">{group.join(", ")}</span>
            </div>
          ))}
        </div>
      ),
      run: async () => {
        await api.partition(groupsToApply);
      },
    });
  };

  const latencyValue = Math.max(0, Math.min(60_000, latency));
  const lossValue = Math.max(0, Math.min(100, loss)) / 100;
  const latencyTargets = latencyTarget ? latencyTarget.split(">") : [];
  const lossTargets = lossTarget ? lossTarget.split(">") : [];

  return (
    <div>
      <PageHeader
        title="Failure simulator"
        description="Faults are injected into real processes and the HTTP mesh. Every crash is SIGKILL, every partition drops real RPCs, and the dashboard reads the resulting state from the nodes."
        actions={
          <>
            <button type="button" className="btn btn-ghost" onClick={() => void refresh()}>
              <RefreshCw size={13} /> Refresh
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() =>
                confirmAction({
                  title: "Clear all injected faults?",
                  description: "Network partitions, latency, packet loss and the random-failure schedule are removed. Any nodes crashed by the random experiment are restarted.",
                  confirmLabel: "Reset simulation",
                  tone: "primary",
                  run: async () => {
                    await api.resetSimulation();
                  },
                })
              }
            >
              <RotateCcw size={13} /> Reset all faults
            </button>
          </>
        }
      />

      {message ? (
        <div className="mb-3">
          <InlineAlert tone="success" title="Command applied">{message}</InlineAlert>
        </div>
      ) : null}
      {error ? (
        <div className="mb-3">
          <InlineAlert tone="error" title="Simulation error">{error}</InlineAlert>
        </div>
      ) : null}

      <div className="mb-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <SimStat label="Cluster" value={cluster.status === "running" ? cluster.health : "stopped"} icon={<Activity size={14} />} tone={cluster.health === "healthy" ? "green" : cluster.health === "critical" || cluster.health === "stopped" ? "red" : "amber"} />
        <SimStat label="Leader" value={leader ?? "none"} icon={<ShieldCheck size={14} />} tone={leader ? "cyan" : "red"} />
        <SimStat label="Live nodes" value={`${cluster.active_nodes}/${cluster.total_nodes}`} icon={<Users size={14} />} tone={cluster.active_nodes >= cluster.quorum_size ? "green" : "red"} />
        <SimStat label="Random failures" value={randomState?.running ? "running" : "idle"} icon={<Dices size={14} />} tone={randomState?.running ? "amber" : "neutral"} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="panel">
          <div className="panel-header">
            <SectionHeading title="Process failures" meta="real SIGKILL / restart" />
            <span className={classNames("chip", cluster.status === "running" ? "chip-green" : "chip-neutral")}>
              {cluster.status}
            </span>
          </div>
          <div className="divide-y divide-console-800/70">
            {nodes.map((node) => (
              <div key={node.node_id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <StatusDot tone={!node.online ? "red" : node.node_id === leader ? "cyan" : "green"} pulse={node.online} />
                <div className="min-w-[92px]">
                  <div className="font-mono text-xs font-semibold text-console-100">{node.node_id}</div>
                  <div className="font-mono text-[9px] text-console-500">pid {node.pid ?? "—"}</div>
                </div>
                <RoleBadge role={node.online ? node.role : "offline"} />
                <div className="hidden text-[10px] text-console-500 sm:block">
                  term {node.term} · log {formatNumber(node.last_log_index)} · crashes {node.crash_count ?? 0}
                </div>
                <div className="ml-auto flex gap-1.5">
                  <button
                    type="button"
                    className="btn btn-danger btn-xs"
                    disabled={!node.online}
                    onClick={() =>
                      confirmAction({
                        title: `SIGKILL ${node.node_id}?`,
                        description: node.node_id === leader
                          ? "This is the current leader. The majority must elect a new leader before writes can commit."
                          : "The node process is terminated immediately and stops receiving RPCs. Its durable log remains on disk.",
                        confirmLabel: "Kill process",
                        tone: "danger",
                        run: async () => {
                          await api.crashNode(node.node_id);
                        },
                      })
                    }
                  >
                    <Skull size={10} /> Kill
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-xs"
                    disabled={node.online}
                    onClick={() =>
                      confirmAction({
                        title: `Restart ${node.node_id}?`,
                        description: "The process reloads its durable term, vote and log from SQLite, then rejoins the cluster.",
                        confirmLabel: "Restart node",
                        tone: "primary",
                        run: async () => {
                          await api.restartNode(node.node_id);
                        },
                      })
                    }
                  >
                    <RotateCcw size={10} /> Restart
                  </button>
                </div>
              </div>
            ))}
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-console-700/60 bg-console-950/30 px-4 py-3">
            <div className="text-[11px] text-console-400">
              Trigger the exact leader-crash scenario used by experiments.
            </div>
            <button
              type="button"
              className="btn btn-danger"
              disabled={!leader || cluster.status !== "running"}
              onClick={() =>
                confirmAction({
                  title: `Crash leader ${leader ?? ""}?`,
                  description: "The leader is SIGKILLed. The controller waits for a new leader to be elected; the old leader is not restarted automatically.",
                  confirmLabel: "Crash leader",
                  tone: "danger",
                  run: async () => {
                    await api.leaderFailure();
                  },
                })
              }
            >
              <Zap size={12} /> Fail current leader
            </button>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <SectionHeading title="Network partition" meta="assign every node to a group" />
            <Layers size={13} className="text-console-500" />
          </div>
          <div className="p-4">
            <div className="mb-3 flex flex-wrap items-center gap-2">
              {nodes.map((node) => {
                const group = groupAssignments[node.node_id];
                return (
                  <div key={node.node_id} className="flex items-center gap-1 rounded-md border border-console-700 bg-console-950 px-2 py-1.5">
                    <span className="font-mono text-[11px] text-console-100">{node.node_id}</span>
                    <div className="ml-1 flex overflow-hidden rounded border border-console-600">
                      {([1, 2, 3] as GroupId[]).map((value) => (
                        <button
                          key={value}
                          type="button"
                          className={classNames(
                            "px-1.5 py-0.5 font-mono text-[9px]",
                            group === value
                              ? value === 1
                                ? "bg-accent-500/25 text-accent-300"
                                : value === 2
                                  ? "bg-signal-violet/25 text-signal-violet"
                                  : "bg-signal-amber/25 text-signal-amber"
                              : "text-console-500 hover:bg-console-800",
                          )}
                          onClick={() => setGroups((current) => ({ ...current, [node.node_id]: value }))}
                        >
                          {value === 1 ? "A" : value === 2 ? "B" : "C"}
                        </button>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="mb-4 grid grid-cols-3 gap-2">
              {([1, 2, 3] as GroupId[]).map((group) => (
                <div key={group} className="rounded border border-console-700 bg-console-950/60 p-2">
                  <div className="mb-1 text-[9px] uppercase tracking-wider text-console-500">Group {group === 1 ? "A" : group === 2 ? "B" : "C"}</div>
                  <div className="font-mono text-[10px] text-console-200">
                    {partitionGroups[group].length ? partitionGroups[group].join(", ") : "empty"}
                  </div>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              <button type="button" className="btn btn-danger" onClick={applyPartition}>
                <WifiOff size={12} /> Apply partition
              </button>
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!network?.partitions.length}
                onClick={() =>
                  confirmAction({
                    title: "Heal the partition?",
                    description: "All partition rules are removed. Isolated nodes can exchange RPCs and catch up again.",
                    confirmLabel: "Heal network",
                    tone: "primary",
                    run: async () => {
                      await api.heal();
                    },
                  })
                }
              >
                <CheckCircle2 size={12} /> Heal
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => setGroups({})}>
                <RotateCcw size={12} /> Reset groups
              </button>
            </div>
            <div className="mt-3 text-[10px] leading-relaxed text-console-500">
              Groups A/B/C define isolated islands. The controller still reaches every proxy over the
              management path, so isolated nodes remain inspectable.
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <SectionHeading title="Link latency" meta="one-way delay" />
            <Gauge size={13} className="text-console-500" />
          </div>
          <div className="space-y-4 p-4">
            <div>
              <label className="label" htmlFor="latency-value">Delay (ms)</label>
              <div className="flex flex-wrap gap-1.5">
                {LATENCY_PRESETS.map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={classNames("btn btn-xs", latency === value ? "btn-primary" : "btn-ghost")}
                    onClick={() => setLatency(value)}
                  >
                    {value} ms
                  </button>
                ))}
                <input
                  id="latency-value"
                  type="number"
                  min={0}
                  max={60000}
                  step={25}
                  className="input w-28 py-1 text-xs"
                  value={latency}
                  onChange={(event) => setLatency(asNumber(event.target.value, 0))}
                />
              </div>
            </div>
            <div>
              <label className="label" htmlFor="latency-target">Scope</label>
              <select
                id="latency-target"
                className="select"
                value={latencyTarget}
                onChange={(event) => setLatencyTarget(event.target.value)}
              >
                <option value="">Every inter-node link</option>
                {nodes.flatMap((source) =>
                  nodes
                    .filter((target) => target.node_id !== source.node_id)
                    .map((target) => (
                      <option key={`${source.node_id}>${target.node_id}`} value={`${source.node_id}>${target.node_id}`}>
                        {source.node_id} → {target.node_id}
                      </option>
                    )),
                )}
              </select>
            </div>
            <button
              type="button"
              className="btn btn-primary w-full"
              onClick={() =>
                confirmAction({
                  title: `Apply ${formatNumber(latencyValue)} ms latency?`,
                  description: "RPCs on the selected link(s) will be delayed before reaching the target. Heartbeats may time out and trigger elections if the delay exceeds election timeouts.",
                  confirmLabel: "Inject latency",
                  tone: "danger",
                  run: async () => {
                    await api.setLatency(latencyValue, latencyTargets[0], latencyTargets[1]);
                  },
                })
              }
            >
              <Clock3 size={12} /> Inject delay
            </button>
            <div className="flex items-center justify-between rounded border border-console-700 bg-console-950/60 px-2.5 py-2 font-mono text-[10px]">
              <span className="text-console-500">current global</span>
              <span className="text-console-200">{formatNumber(network?.default_latency_ms ?? 0)} ms</span>
            </div>
          </div>
        </section>

        <section className="panel">
          <div className="panel-header">
            <SectionHeading title="Packet loss" meta="probabilistic drop" />
            <Ban size={13} className="text-console-500" />
          </div>
          <div className="space-y-4 p-4">
            <div>
              <label className="label" htmlFor="loss-value">Drop probability</label>
              <div className="flex flex-wrap gap-1.5">
                {LOSS_PRESETS.map((value) => (
                  <button
                    key={value}
                    type="button"
                    className={classNames("btn btn-xs", loss === value ? "btn-primary" : "btn-ghost")}
                    onClick={() => setLoss(value)}
                  >
                    {value}%
                  </button>
                ))}
                <input
                  id="loss-value"
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  className="input w-24 py-1 text-xs"
                  value={loss}
                  onChange={(event) => setLoss(asNumber(event.target.value, 0))}
                />
              </div>
            </div>
            <div>
              <label className="label" htmlFor="loss-target">Scope</label>
              <select
                id="loss-target"
                className="select"
                value={lossTarget}
                onChange={(event) => setLossTarget(event.target.value)}
              >
                <option value="">Every inter-node link</option>
                {nodes.flatMap((source) =>
                  nodes
                    .filter((target) => target.node_id !== source.node_id)
                    .map((target) => (
                      <option key={`${source.node_id}>${target.node_id}`} value={`${source.node_id}>${target.node_id}`}>
                        {source.node_id} → {target.node_id}
                      </option>
                    )),
                )}
              </select>
            </div>
            <button
              type="button"
              className="btn btn-primary w-full"
              onClick={() =>
                confirmAction({
                  title: `Apply ${lossValue * 100}% packet loss?`,
                  description: "Each RPC on the selected link has the configured probability of being dropped before it reaches the target. Dropped messages are real network failures, not client-side errors.",
                  confirmLabel: "Inject packet loss",
                  tone: "danger",
                  run: async () => {
                    await api.setPacketLoss(lossValue, lossTargets[0], lossTargets[1]);
                  },
                })
              }
            >
              <ArrowLeftRight size={12} /> Inject packet loss
            </button>
            <div className="flex items-center justify-between rounded border border-console-700 bg-console-950/60 px-2.5 py-2 font-mono text-[10px]">
              <span className="text-console-500">current global</span>
              <span className="text-console-200">{Math.round((network?.default_loss ?? 0) * 100)}%</span>
            </div>
          </div>
        </section>

        <section className="panel xl:col-span-2">
          <div className="panel-header">
            <SectionHeading title="Random failure experiment" meta="seeded and reproducible" />
            <Dices size={13} className="text-console-500" />
          </div>
          <div className="grid gap-4 p-4 lg:grid-cols-[1fr_290px]">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              <Field label="Failure probability">
                <input
                  type="number"
                  min={0.01}
                  max={1}
                  step={0.05}
                  className="input"
                  value={probability}
                  onChange={(event) => setProbability(asNumber(event.target.value, 0.35))}
                />
              </Field>
              <Field label="Check interval (s)">
                <input
                  type="number"
                  min={0.2}
                  max={60}
                  step={0.2}
                  className="input"
                  value={intervalS}
                  onChange={(event) => setIntervalS(asNumber(event.target.value, 4))}
                />
              </Field>
              <Field label="Recovery delay (s)">
                <input
                  type="number"
                  min={0}
                  max={120}
                  step={0.5}
                  className="input"
                  value={recoveryDelayS}
                  onChange={(event) => setRecoveryDelayS(asNumber(event.target.value, 4))}
                />
              </Field>
              <Field label="Maximum failures">
                <input
                  type="number"
                  min={1}
                  max={50}
                  className="input"
                  value={maxFailures}
                  onChange={(event) => setMaxFailures(Math.round(asNumber(event.target.value, 2)))}
                />
              </Field>
              <Field label="Random seed">
                <input
                  type="number"
                  className="input"
                  value={seed}
                  onChange={(event) => setSeed(Math.round(asNumber(event.target.value, 1234)))}
                />
              </Field>
              <Field label="Duration (s)">
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    min={1}
                    max={600}
                    className="input"
                    value={durationS}
                    disabled={!useDuration}
                    onChange={(event) => setDurationS(asNumber(event.target.value, 30))}
                  />
                  <label className="flex shrink-0 items-center gap-1 text-[10px] text-console-400">
                    <input type="checkbox" checked={useDuration} onChange={(event) => setUseDuration(event.target.checked)} />
                    limit
                  </label>
                </div>
              </Field>
            </div>
            <div className="flex flex-col gap-2">
              <div className="rounded border border-console-700 bg-console-950/60 p-3 font-mono text-[10px] text-console-400">
                <div className="flex justify-between"><span>status</span><span className={randomState?.running ? "text-signal-amber" : "text-console-200"}>{randomState?.running ? "running" : "idle"}</span></div>
                <div className="mt-1 flex justify-between"><span>failures</span><span className="text-console-200">{randomState?.failures ?? 0}</span></div>
                <div className="mt-1 flex justify-between"><span>recoveries</span><span className="text-console-200">{randomState?.recoveries ?? 0}</span></div>
                <div className="mt-1 flex justify-between"><span>seed</span><span className="text-console-200">{randomState?.config?.seed ?? "—"}</span></div>
              </div>
              {randomState?.running ? (
                <button
                  type="button"
                  className="btn btn-danger"
                  onClick={() =>
                    confirmAction({
                      title: "Stop random failures?",
                      description: "The schedule is cancelled and every node crashed by this experiment is restarted. Network faults are left untouched.",
                      confirmLabel: "Stop experiment",
                      tone: "danger",
                      run: async () => {
                        await api.stopRandomFailures();
                      },
                    })
                  }
                >
                  <PauseCircle size={12} /> Stop experiment
                </button>
              ) : (
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={cluster.status !== "running"}
                  onClick={() =>
                    confirmAction({
                      title: "Start random failure experiment?",
                      description: `Every ${intervalS}s each live node has a ${Math.round(probability * 100)}% chance of being SIGKILLed. Up to ${maxFailures} nodes fail, with automatic restart after ${recoveryDelayS}s.`,
                      confirmLabel: "Start failures",
                      tone: "danger",
                      detail: (
                        <div className="rounded border border-console-700 bg-console-950 p-3 font-mono text-[10px] text-console-400">
                          seed <span className="text-console-100">{seed}</span> · interval {intervalS}s · recovery {recoveryDelayS}s · {useDuration ? `${durationS}s duration` : "until stopped"}
                        </div>
                      ),
                      run: async () => {
                        await api.startRandomFailures({
                          probability,
                          interval_s: intervalS,
                          recovery_delay_s: recoveryDelayS,
                          max_failures: maxFailures,
                          seed,
                          duration_s: useDuration ? durationS : null,
                        });
                      },
                    })
                  }
                >
                  <Play size={12} /> Start random failures
                </button>
              )}
              <button
                type="button"
                className="btn btn-ghost"
                disabled={!network || (!network.partitions.length && !network.links.length && !network.default_latency_ms && !network.default_loss)}
                onClick={() =>
                  confirmAction({
                    title: "Remove all network faults?",
                    description: "Partitions, latency and packet loss are cleared. Random failure scheduling is also stopped and crashed nodes are restarted.",
                    confirmLabel: "Clear faults",
                    tone: "primary",
                    run: async () => {
                      await api.resetSimulation();
                    },
                  })
                }
              >
                <RotateCcw size={12} /> Clear network faults
              </button>
            </div>
          </div>
        </section>
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[1fr_360px]">
        <section className="panel">
          <div className="panel-header">
            <SectionHeading title="Active fault state" meta="as reported by the injector" />
            <span className={classNames("chip", activeFaults.length ? "chip-amber" : "chip-green")}>
              {activeFaults.length ? `${activeFaults.length} active` : "clean"}
            </span>
          </div>
          {activeFaults.length ? (
            <div className="divide-y divide-console-800/60">
              {activeFaults.map((fault) => (
                <div key={fault} className="flex items-center gap-2 px-4 py-2.5 text-[11px] text-console-200">
                  <TriangleAlert size={12} className="text-signal-amber" />
                  <span className="font-mono">{fault}</span>
                </div>
              ))}
            </div>
          ) : (
            <EmptyState title="No active faults" description="The cluster is running with an unmodified inter-node network." icon={<ShieldCheck size={20} />} />
          )}
        </section>

        <section className="panel">
          <div className="panel-header">
            <SectionHeading title="Cluster lifecycle" meta="controller managed" />
            <Power size={13} className="text-console-500" />
          </div>
          <div className="space-y-3 p-4">
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-console-400">Status</span>
              <span className={classNames("font-mono", cluster.status === "running" ? "text-signal-green" : "text-console-400")}>{cluster.status}</span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-console-400">Uptime</span>
              <span className="font-mono text-console-200">{formatDuration(cluster.uptime_s)}</span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-console-400">Election duration</span>
              <span className="font-mono text-console-200">{cluster.election_ms === null ? "—" : formatMs(cluster.election_ms)}</span>
            </div>
            <div className="flex items-center justify-between text-[11px]">
              <span className="text-console-400">In-flight client ops</span>
              <span className="font-mono text-console-200">{cluster.simulation.inflight_operations}</span>
            </div>
            {cluster.status === "running" ? (
              <button
                type="button"
                className="btn btn-danger w-full"
                onClick={() =>
                  confirmAction({
                    title: "Stop the entire cluster?",
                    description: "All node processes are terminated. Durable logs remain on disk; starting again recovers terms, votes and logs.",
                    confirmLabel: "Stop cluster",
                    tone: "danger",
                    run: async () => {
                      await api.stopCluster();
                    },
                  })
                }
              >
                <Power size={12} /> Stop cluster
              </button>
            ) : (
              <button
                type="button"
                className="btn btn-primary w-full"
                onClick={() =>
                  confirmAction({
                    title: "Start the cluster?",
                    description: "Node processes are launched with their existing data directories. The cluster elects a leader and recovers committed state.",
                    confirmLabel: "Start cluster",
                    tone: "primary",
                    run: async () => {
                      await api.startCluster();
                    },
                  })
                }
              >
                <Play size={12} /> Start cluster
              </button>
            )}
            <div className="flex items-center gap-2 rounded border border-console-700 bg-console-950/60 px-2.5 py-2 text-[10px] text-console-500">
              <AlertOctagon size={11} />
              There is no shortcut for quorum: corrupting the majority stops commits.
            </div>
          </div>
        </section>
      </div>

      <div className="mt-4 rounded-lg border border-console-700/60 bg-console-900/50 px-4 py-3">
        <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-console-300">
          <SlidersHorizontal size={12} /> Reproducibility
        </div>
        <p className="text-[11px] leading-relaxed text-console-400">
          Random failures use a seeded Python PRNG: the same node set, seed and probability
          produce the same decisions in sequence. Network drops use the simulator's own seeded PRNG.
          Every command here is recorded in the event timeline with the resulting state.
        </p>
      </div>

      <ConfirmDialog
        open={pending !== null}
        title={pending?.title ?? ""}
        description={pending?.description}
        confirmLabel={pending?.confirmLabel}
        tone={pending?.tone}
        busy={busy}
        onConfirm={() => {
          if (pending) void execute(pending.title, pending.run);
        }}
        onClose={() => !busy && setPending(null)}
      >
        {pending?.detail}
      </ConfirmDialog>
    </div>
  );
}

function SimStat({
  label,
  value,
  icon,
  tone,
}: {
  label: string;
  value: string;
  icon: React.ReactNode;
  tone: "neutral" | "green" | "red" | "amber" | "cyan";
}) {
  const colors = {
    neutral: "text-console-100",
    green: "text-signal-green",
    red: "text-signal-red",
    amber: "text-signal-amber",
    cyan: "text-accent-300",
  };
  return (
    <div className="panel flex items-center gap-3 px-3 py-3">
      <span className="text-console-500">{icon}</span>
      <div className="min-w-0">
        <div className="text-[9px] uppercase tracking-[0.14em] text-console-500">{label}</div>
        <div className={classNames("truncate font-mono text-sm font-semibold", colors[tone])}>{value}</div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <span className="label">{label}</span>
      {children}
    </div>
  );
}
