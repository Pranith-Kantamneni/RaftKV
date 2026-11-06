import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowLeftRight,
  CheckCircle2,
  ChevronRight,
  CircleSlash,
  Clock3,
  Cpu,
  Download,
  FileText,
  FlaskConical,
  Gauge,
  GitCommitHorizontal,
  Loader2,
  Play,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Timer,
  TriangleAlert,
  XCircle,
  Zap,
} from "lucide-react";
import { api } from "../services/api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EventList } from "../components/EventList";
import {
  EmptyState,
  InlineAlert,
  PageHeader,
  SectionHeading,
  Spinner,
  StatusDot,
} from "../components/ui";
import {
  classNames,
  downloadJson,
  formatDateTime,
  formatDuration,
  formatMs,
  formatNumber,
} from "../utils/format";
import type {
  ExperimentAssertion,
  ExperimentReport,
  ExperimentSummary,
  ConsistencyCheck,
} from "../types";

const SCENARIOS = [
  { id: "normal", label: "Normal operation", description: "No faults. Establishes the baseline." },
  { id: "follower_down", label: "Follower crash", description: "Kill one follower, then restart it." },
  { id: "leader_crash", label: "Leader crash", description: "Kill the leader; wait for failover; rejoin it." },
  { id: "latency", label: "Network latency", description: "Inject 200 ms on every inter-node link." },
  { id: "packet_loss", label: "Packet loss", description: "Drop 25% of inter-node messages." },
  { id: "partition", label: "Network partition", description: "Isolate the leader, verify minority rejection." },
] as const;

type ScenarioId = (typeof SCENARIOS)[number]["id"];

export function ReportsPage() {
  const [reports, setReports] = useState<ExperimentSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [report, setReport] = useState<ExperimentReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [runOpen, setRunOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [scenario, setScenario] = useState<ScenarioId>("normal");
  const [duration, setDuration] = useState(12);
  const [rate, setRate] = useState(6);

  const loadList = async (preferId?: string) => {
    try {
      const payload = await api.experiments();
      setReports(payload.experiments);
      const nextId = preferId ?? selectedId ?? payload.experiments[0]?.id ?? null;
      setSelectedId(nextId);
      if (nextId) await loadDetail(nextId);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load experiment reports");
    } finally {
      setLoading(false);
    }
  };

  const loadDetail = async (id: string) => {
    setDetailLoading(true);
    try {
      const payload = await api.experiment(id);
      setReport(payload);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load report");
      setReport(null);
    } finally {
      setDetailLoading(false);
    }
  };

  useEffect(() => {
    void loadList();
  }, []);

  const filteredReports = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return reports;
    return reports.filter((item) =>
      `${item.scenario} ${item.kind} ${item.id} ${item.outcome}`.toLowerCase().includes(needle),
    );
  }, [reports, query]);

  const select = (id: string) => {
    setSelectedId(id);
    setReport(null);
    void loadDetail(id);
  };

  const run = async () => {
    setRunning(true);
    setError(null);
    try {
      const result = await api.runExperiment(scenario, duration, rate);
      const summary: ExperimentSummary = {
        id: result.id,
        created_at: result.created_at,
        scenario: result.config.scenario,
        kind: result.kind,
        duration_s: result.config.duration_s,
        outcome: result.outcome,
        consistency_passed: result.consistency?.passed ?? false,
        summary: result.summary,
      };
      setReports((current) => [summary, ...current.filter((item) => item.id !== result.id)]);
      setSelectedId(result.id);
      setReport(result);
      setRunOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Experiment failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <PageHeader
        title="Experiment reports"
        description="Every report is produced by executing a workload against live node processes while faults are injected. Reports include assertions, sampled Raft safety checks, recovery facts and the raw event timeline."
        actions={
          <>
            <button type="button" className="btn btn-ghost" onClick={() => void loadList()} disabled={loading}>
              <RefreshCw size={13} className={loading ? "animate-spin" : ""} /> Refresh
            </button>
            <button type="button" className="btn btn-primary" onClick={() => setRunOpen(true)}>
              <Play size={13} /> Run experiment
            </button>
          </>
        }
      />

      {error ? <div className="mb-3"><InlineAlert tone="error" title="Report error">{error}</InlineAlert></div> : null}

      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)]">
        <div className="panel flex max-h-[calc(100vh-180px)] flex-col overflow-hidden">
          <div className="border-b border-console-700/60 p-3">
            <div className="relative">
              <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-console-500" />
              <input
                className="input py-1.5 pl-7 text-xs"
                placeholder="Search reports…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                aria-label="Search reports"
              />
            </div>
            <div className="mt-2 flex items-center justify-between font-mono text-[10px] text-console-500">
              <span>{filteredReports.length} reports</span>
              <span>{reports.filter((item) => item.outcome === "passed").length} passed</span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {loading && !reports.length ? (
              <div className="flex items-center justify-center gap-2 p-8 text-xs text-console-400"><Spinner /> Loading reports…</div>
            ) : filteredReports.length ? (
              filteredReports.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => select(item.id)}
                  className={classNames(
                    "flex w-full items-start gap-2.5 border-b border-console-800/60 px-3 py-2.5 text-left transition-colors hover:bg-console-850/70",
                    selectedId === item.id && "bg-accent-500/[0.06]",
                  )}
                >
                  <StatusDot
                    tone={item.outcome === "passed" ? "green" : item.outcome === "failed" ? "red" : "amber"}
                    pulse={selectedId === item.id}
                    className="mt-1.5"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-mono text-[11px] font-medium text-console-100">{item.scenario}</span>
                      <span className={classNames("chip shrink-0 text-[8px]", item.outcome === "passed" ? "chip-green" : "chip-red")}>{item.outcome}</span>
                    </div>
                    <div className="mt-1 truncate font-mono text-[9px] text-console-500">{item.id}</div>
                    <div className="mt-1 flex flex-wrap gap-x-2 text-[9px] text-console-500">
                      <span>{formatDateTime(item.created_at)}</span>
                      <span>{item.duration_s ?? "—"}s</span>
                      {item.kind === "benchmark" || item.kind === "benchmark-scenario" ? <span className="text-signal-violet">{item.kind}</span> : null}
                      {item.summary?.throughput_ops_s !== undefined ? <span className="text-console-400">{formatNumber(item.summary.throughput_ops_s, 1)} ops/s</span> : null}
                    </div>
                  </div>
                  <ChevronRight size={11} className={classNames("mt-1 text-console-600", selectedId === item.id && "text-accent-400")} />
                </button>
              ))
            ) : (
              <EmptyState title={reports.length ? "No matching reports" : "No reports yet"} description={reports.length ? "Clear the search filter." : "Run an experiment to generate the first report."} />
            )}
          </div>
        </div>

        <div className="min-w-0">
          {detailLoading && !report ? (
            <div className="panel flex min-h-[420px] items-center justify-center gap-2 text-xs text-console-400">
              <Loader2 size={15} className="animate-spin" /> Loading report detail…
            </div>
          ) : report ? (
            <ReportDetail report={report} onSelectReport={select} />
          ) : (
            <div className="panel flex min-h-[420px] items-center justify-center">
              <EmptyState
                icon={<FileText size={24} />}
                title="Select a report"
                description="Choose an experiment on the left, or execute a new scenario against the live cluster."
                action={<button type="button" className="btn btn-primary" onClick={() => setRunOpen(true)}><Play size={12} /> Run experiment</button>}
              />
            </div>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={runOpen}
        title="Execute experiment against live cluster?"
        description="The runner starts a client workload, injects the selected fault at ~30% of the duration, then waits for convergence and checks Raft safety properties. This affects the running cluster."
        confirmLabel="Run experiment"
        tone="danger"
        busy={running}
        onConfirm={() => void run()}
        onClose={() => !running && setRunOpen(false)}
      >
        <div className="space-y-3">
          <div>
            <label className="label" htmlFor="report-scenario">Scenario</label>
            <select id="report-scenario" className="select" value={scenario} onChange={(event) => setScenario(event.target.value as ScenarioId)}>
              {SCENARIOS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
            <p className="mt-1 text-[10px] text-console-500">{SCENARIOS.find((item) => item.id === scenario)?.description}</p>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="report-duration">Duration (s)</label>
              <input
                id="report-duration"
                type="number"
                min={3}
                max={120}
                className="input"
                value={duration}
                onChange={(event) => setDuration(Math.max(3, Math.min(120, Number(event.target.value) || 12)))}
              />
            </div>
            <div>
              <label className="label" htmlFor="report-rate">Target rate (ops/s)</label>
              <input
                id="report-rate"
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
          {running ? <div className="flex items-center gap-2 text-[11px] text-accent-300"><Spinner /> Executing workload and converging…</div> : null}
        </div>
      </ConfirmDialog>
    </div>
  );
}

function ReportDetail({ report, onSelectReport }: { report: ExperimentReport; onSelectReport: (id: string) => void }) {
  const isSuite = report.kind === "benchmark" || Boolean(report.scenarios);
  const assertions = report.assertions ?? [];
  const passedAssertions = assertions.filter((item) => item.passed).length;
  const checks = Object.entries(report.consistency?.checks ?? {});
  const violations = report.consistency?.violations ?? [];
  const outcomeOk = report.outcome === "passed";

  return (
    <div className="space-y-4">
      <div className="panel">
        <div className="flex flex-col gap-3 border-b border-console-700/60 p-4 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-mono text-sm font-semibold text-console-100">{report.config.scenario}</h2>
              <span className={classNames("chip", outcomeOk ? "chip-green" : "chip-red")}>
                {outcomeOk ? <CheckCircle2 size={10} /> : <XCircle size={10} />} {report.outcome}
              </span>
              <span className="chip chip-neutral">{report.kind}</span>
              {isSuite ? <span className="chip chip-violet">suite</span> : null}
            </div>
            <div className="mt-1 font-mono text-[10px] text-console-500">{report.id}</div>
            <div className="mt-1 text-[10px] text-console-500">{formatDateTime(report.created_at)}{report.finished_at ? ` → ${formatDateTime(report.finished_at)}` : ""}</div>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => downloadJson(`${report.id}.json`, report)}>
              <Download size={11} /> JSON
            </button>
          </div>
        </div>
        {report.config.description ? (
          <div className="px-4 py-3 text-[11px] leading-relaxed text-console-400">{report.config.description}</div>
        ) : null}
      </div>

      {isSuite ? (
        <SuiteDetail report={report} onSelectReport={onSelectReport} />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3 xl:grid-cols-6">
            <ReportStat label="Throughput" value={`${formatNumber(report.summary?.throughput_ops_s ?? report.workload?.throughput_ops_s, 2)}`} unit="ops/s" icon={<Zap size={12} />} tone="cyan" />
            <ReportStat label="P95 latency" value={formatMs(report.summary?.p95_ms ?? report.workload?.latency?.write_p95_ms)} icon={<Timer size={12} />} />
            <ReportStat label="P99 latency" value={formatMs(report.summary?.p99_ms ?? report.workload?.latency?.write_p99_ms)} icon={<Clock3 size={12} />} />
            <ReportStat label="Write failures" value={formatNumber(report.summary?.write_failures ?? report.workload?.write_failures)} icon={<TriangleAlert size={12} />} tone={(report.summary?.write_failures ?? 0) > 0 ? "amber" : "green"} />
            <ReportStat label="Recovery" value={report.recovery?.recovery_s === null || report.recovery?.recovery_s === undefined ? "—" : `${formatNumber(report.recovery.recovery_s, 2)} s`} icon={<Activity size={12} />} />
            <ReportStat label="Consistency" value={report.consistency?.passed ? "passed" : "failed"} icon={report.consistency?.passed ? <ShieldCheck size={12} /> : <ShieldAlert size={12} />} tone={report.consistency?.passed ? "green" : "red"} />
          </div>

          <div className="panel">
            <div className="panel-header">
              <SectionHeading title="Assertions" meta={`${passedAssertions}/${assertions.length} passed`} />
              <span className={classNames("chip", passedAssertions === assertions.length && assertions.length > 0 ? "chip-green" : "chip-red")}>
                {passedAssertions === assertions.length && assertions.length > 0 ? "all passed" : "review"}
              </span>
            </div>
            <div className="divide-y divide-console-800/60">
              {assertions.map((item) => <AssertionRow key={item.name} assertion={item} />)}
              {!assertions.length ? <div className="px-4 py-6 text-center text-xs text-console-500">No assertions recorded.</div> : null}
            </div>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="panel">
              <div className="panel-header"><SectionHeading title="Workload" meta="client observed" /><Gauge size={13} className="text-console-500" /></div>
              <div className="grid grid-cols-2 gap-3 p-4">
                <DataLine label="Attempts" value={formatNumber(report.workload?.write_attempts)} />
                <DataLine label="Successes" value={formatNumber(report.workload?.write_successes)} />
                <DataLine label="Failures" value={formatNumber(report.workload?.write_failures)} warn={(report.workload?.write_failures ?? 0) > 0} />
                <DataLine label="Acknowledged keys" value={formatNumber(report.workload?.acknowledged_keys)} />
                <DataLine label="Reads" value={`${formatNumber(report.workload?.read_successes)} ok / ${formatNumber(report.workload?.read_failures)} fail`} />
                <DataLine label="Cluster commit rate" value={`${formatNumber(report.workload?.cluster_commit_throughput_ops_s, 2)} ops/s`} />
                <DataLine label="Measurement window" value={formatDuration(report.workload?.measurement_window_s)} />
                <DataLine label="Target rate" value={`${formatNumber(report.workload?.target_rate_ops_s, 1)} ops/s`} />
                <DataLine label="Avg write" value={formatMs(report.workload?.latency?.write_avg_ms)} />
                <DataLine label="P50 write" value={formatMs(report.workload?.latency?.write_p50_ms)} />
                <DataLine label="P95 write" value={formatMs(report.workload?.latency?.write_p95_ms)} />
                <DataLine label="P99 write" value={formatMs(report.workload?.latency?.write_p99_ms)} />
                <DataLine label="Avg read" value={formatMs(report.workload?.latency?.read_avg_ms)} />
                <DataLine label="P95 read" value={formatMs(report.workload?.latency?.read_p95_ms)} />
              </div>
              {report.workload?.sample_errors?.length ? (
                <div className="border-t border-console-700/60 p-3">
                  <div className="mb-1 text-[9px] uppercase tracking-wider text-console-500">Sample errors</div>
                  <pre className="max-h-24 overflow-auto whitespace-pre-wrap rounded bg-console-950 p-2 font-mono text-[9px] text-signal-red/80">{report.workload.sample_errors.join("\n")}</pre>
                </div>
              ) : null}
            </div>

            <div className="panel">
              <div className="panel-header"><SectionHeading title="Recovery & final state" meta="post-fault convergence" /><GitCommitHorizontal size={13} className="text-console-500" /></div>
              <div className="grid grid-cols-2 gap-3 p-4">
                <DataLine label="Fault window" value={formatDuration(report.recovery?.fault_window_s)} />
                <DataLine label="Recovery" value={report.recovery?.recovery_s === null || report.recovery?.recovery_s === undefined ? "—" : `${formatNumber(report.recovery.recovery_s, 2)} s`} />
                <DataLine label="Election delay" value={formatMs(report.recovery?.election_ms)} />
                <DataLine label="Final leader" value={report.recovery?.final_leader ?? "none"} />
                <DataLine label="Final term" value={String(report.recovery?.final_term ?? "—")} />
                <DataLine label="Final commit" value={formatNumber(report.recovery?.final_commit_index)} />
                <DataLine label="Final applied" value={formatNumber(report.recovery?.final_applied_index)} />
                <DataLine label="Cluster commits" value={formatNumber(report.recovery?.cluster_commits)} />
                <DataLine label="Converged" value={report.final_state?.converged ? "yes" : "no"} warn={!report.final_state?.converged} />
                <DataLine label="KV entries" value={formatNumber(report.final_state?.kv_count)} />
                <DataLine label="Ack state error" value={report.final_state?.acknowledged_state_error ?? "none"} warn={Boolean(report.final_state?.acknowledged_state_error)} />
              </div>
              {report.final_state?.applied_indexes ? (
                <div className="border-t border-console-700/60 p-3">
                  <div className="mb-1.5 text-[9px] uppercase tracking-wider text-console-500">Applied indexes</div>
                  <div className="grid grid-cols-3 gap-2 font-mono text-[10px] text-console-400">
                    {Object.entries(report.final_state.applied_indexes).map(([node, index]) => (
                      <div key={node} className="flex justify-between rounded border border-console-700 bg-console-950/60 px-2 py-1">
                        <span>{node}</span><span className="text-console-200">{index}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          </div>

          <div className="panel">
            <div className="panel-header">
              <SectionHeading title="Raft safety checks" meta={`${formatNumber(report.consistency?.total_checks_executed)} checks · ${violations.length} violations`} />
              <span className={classNames("chip", report.consistency?.passed ? "chip-green" : "chip-red")}>
                {report.consistency?.passed ? "no violations" : "violations found"}
              </span>
            </div>
            <div className="grid gap-2 p-4 sm:grid-cols-2 lg:grid-cols-4">
              {checks.map(([name, check]) => <CheckCard key={name} name={name} check={check} />)}
              {!checks.length ? <div className="col-span-full text-center text-xs text-console-500">No consistency checks recorded.</div> : null}
            </div>
            {violations.length ? (
              <div className="border-t border-signal-red/20 p-4">
                <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold text-signal-red"><ShieldAlert size={12} /> Violations</div>
                <div className="space-y-2">
                  {violations.map((violation, index) => (
                    <pre key={index} className="overflow-auto whitespace-pre-wrap rounded border border-signal-red/20 bg-signal-red/5 p-2 font-mono text-[9px] text-signal-red/90">
                      {violation.check}: {violation.message}{"\n"}{JSON.stringify(violation.evidence, null, 2)}
                    </pre>
                  ))}
                </div>
              </div>
            ) : null}
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <div className="panel p-4">
              <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-console-300"><Cpu size={12} /> Hardware & topology</div>
              <div className="grid grid-cols-2 gap-2 font-mono text-[10px] text-console-400">
                {Object.entries(report.hardware ?? {}).map(([key, value]) => (
                  <div key={key} className="flex justify-between gap-2 rounded border border-console-700 bg-console-950/60 px-2 py-1">
                    <span>{key}</span><span className="truncate text-console-200">{String(value)}</span>
                  </div>
                ))}
                <div className="flex justify-between gap-2 rounded border border-console-700 bg-console-950/60 px-2 py-1">
                  <span>cluster</span><span className="text-console-200">{report.topology?.cluster_size ?? "—"} nodes / quorum {report.topology?.quorum_size ?? "—"}</span>
                </div>
                <div className="flex justify-between gap-2 rounded border border-console-700 bg-console-950/60 px-2 py-1">
                  <span>initial leader</span><span className="text-console-200">{report.topology?.leader ?? "—"} / term {report.topology?.term ?? "—"}</span>
                </div>
              </div>
              <div className="mt-3 text-[10px] leading-relaxed text-console-500">{report.methodology}</div>
            </div>

            <div className="panel p-4">
              <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-console-300"><FlaskConical size={12} /> Injected faults</div>
              {report.faults && Object.keys(report.faults).length ? (
                <pre className="max-h-52 overflow-auto whitespace-pre-wrap rounded border border-console-700 bg-console-950 p-2.5 font-mono text-[9px] text-console-300">{JSON.stringify(report.faults, null, 2)}</pre>
              ) : (
                <div className="text-[11px] text-console-500">No faults were injected in this scenario.</div>
              )}
            </div>
          </div>

          <div className="panel overflow-hidden">
            <div className="panel-header">
              <SectionHeading title="Report event timeline" meta={`${report.event_timeline?.length ?? 0} captured events`} />
              <span className="font-mono text-[10px] text-console-500">newest first</span>
            </div>
            {report.event_timeline?.length ? (
              <EventList events={[...report.event_timeline].reverse()} dense maxHeight="460px" />
            ) : (
              <EmptyState title="No timeline captured" />
            )}
          </div>
        </>
      )}
    </div>
  );
}

function SuiteDetail({ report, onSelectReport }: { report: ExperimentReport; onSelectReport: (id: string) => void }) {
  const scenarios = report.scenarios ?? [];
  return (
    <>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <ReportStat label="Scenarios" value={String(scenarios.length)} icon={<FlaskConical size={12} />} />
        <ReportStat label="Passed" value={String(scenarios.filter((item) => item.outcome === "passed").length)} icon={<CheckCircle2 size={12} />} tone="green" />
        <ReportStat label="Failed" value={String(scenarios.filter((item) => item.outcome !== "passed").length)} icon={<CircleSlash size={12} />} tone={scenarios.some((item) => item.outcome !== "passed") ? "red" : "neutral"} />
        <ReportStat label="Mode" value="sequential" icon={<ArrowLeftRight size={12} />} />
      </div>
      <div className="panel overflow-hidden">
        <div className="panel-header"><SectionHeading title="Scenario comparison" meta={report.id} /><span className="chip chip-violet">benchmark suite</span></div>
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Scenario</th><th>Outcome</th><th>Throughput</th><th>P50</th><th>P95</th><th>P99</th><th>Failures</th><th>Recovery</th><th>Consistency</th>
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
                  <td className="font-mono">{scenario.recovery_s === null ? "—" : `${formatNumber(scenario.recovery_s, 2)}s`}</td>
                  <td className={classNames("font-mono text-[10px]", scenario.consistency ? "text-signal-green" : "text-signal-red")}>{scenario.consistency ? "passed" : "failed"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="border-t border-console-700/60 p-4">
          <div className="mb-2 text-[9px] uppercase tracking-wider text-console-500">Linked scenario reports</div>
          <div className="flex flex-wrap gap-1.5">
            {(report.report_ids ?? []).map((id) => (
              <button key={id} type="button" className="btn btn-ghost btn-xs font-mono" onClick={() => onSelectReport(id)}>
                {id}
              </button>
            ))}
          </div>
          <div className="mt-3 text-[10px] leading-relaxed text-console-500">{report.methodology}</div>
        </div>
      </div>
    </>
  );
}

function AssertionRow({ assertion }: { assertion: ExperimentAssertion }) {
  return (
    <div className="flex items-start gap-2.5 px-4 py-2.5">
      {assertion.passed ? <CheckCircle2 size={13} className="mt-0.5 shrink-0 text-signal-green" /> : <XCircle size={13} className="mt-0.5 shrink-0 text-signal-red" />}
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[11px] text-console-100">{assertion.name}</div>
        <div className="mt-0.5 text-[10px] text-console-400">{assertion.detail}</div>
      </div>
      <span className={classNames("chip shrink-0", assertion.passed ? "chip-green" : "chip-red")}>{assertion.passed ? "pass" : "fail"}</span>
    </div>
  );
}

function CheckCard({ name, check }: { name: string; check: ConsistencyCheck }) {
  return (
    <div className={classNames("rounded border p-2.5", check.passed ? "border-console-700 bg-console-950/50" : "border-signal-red/30 bg-signal-red/5")}>
      <div className="flex items-center gap-1.5">
        {check.passed ? <ShieldCheck size={11} className="text-signal-green" /> : <ShieldAlert size={11} className="text-signal-red" />}
        <span className="truncate font-mono text-[10px] text-console-200">{name}</span>
      </div>
      <div className="mt-1 flex items-center justify-between text-[9px] text-console-500">
        <span>{formatNumber(check.checked)} checked · {formatNumber(check.skipped)} skipped</span>
        <span className={check.violations ? "text-signal-red" : "text-signal-green"}>{formatNumber(check.violations)} viol.</span>
      </div>
    </div>
  );
}

function ReportStat({
  label,
  value,
  unit,
  icon,
  tone = "neutral",
}: {
  label: string;
  value: string;
  unit?: string;
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
        <span className="text-[9px] uppercase tracking-[0.12em] text-console-500">{label}</span>
        <span className="text-console-600">{icon}</span>
      </div>
      <div className={classNames("mt-1 font-mono text-lg font-semibold", colors[tone])}>
        {value}{unit ? <span className="ml-1 text-[9px] font-normal text-console-500">{unit}</span> : null}
      </div>
    </div>
  );
}

function DataLine({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2 text-[10px]">
      <span className="text-console-500">{label}</span>
      <span className={classNames("truncate font-mono", warn ? "text-signal-red" : "text-console-200")}>{value}</span>
    </div>
  );
}
