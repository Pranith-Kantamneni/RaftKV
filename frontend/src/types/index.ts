export type Role = "leader" | "follower" | "candidate" | "offline";
export type Health = "healthy" | "degraded" | "critical" | "stopped";
export type Severity = "debug" | "info" | "warning" | "error" | "critical";

export interface NodeStatus {
  node_id: string;
  online: boolean;
  process_running: boolean;
  status: "online" | "crashed" | "unreachable";
  role: Role;
  term: number;
  voted_for: string | null;
  leader_id: string | null;
  last_log_index: number;
  last_log_term: number;
  commit_index: number;
  applied_index: number;
  applied_term: number;
  snapshot_index: number;
  snapshot_term: number;
  log_length: number;
  cluster_size: number;
  quorum_size: number;
  peers: string[];
  next_index: Record<string, number>;
  match_index: Record<string, number>;
  last_ack_age_ms: Record<string, number | null>;
  quorum_fresh: number;
  election_timeout_remaining_ms: number;
  last_contact_age_ms: number;
  uptime_s: number;
  kv_count: number;
  events_seq: number;
  counters: Record<string, number>;
  recent_commit_latency_ms: number[];
  avg_rpc_latency_ms: number;
  persistence: { db_bytes?: number; wal_bytes?: number; log_rows?: number; snapshot_bytes?: number };
  pid?: number | null;
  crash_count?: number;
  restarts?: number;
  http_port: number;
  proxy_port: number;
  last_seen_ms: number | null;
  inflight_operations?: number;
}

export interface NetworkLink {
  source: string;
  target: string;
  latency_ms: number;
  loss: number;
}

export interface NetworkState {
  partitions: string[][];
  default_latency_ms: number;
  default_loss: number;
  links: NetworkLink[];
}

export interface RandomFailureConfig {
  probability: number;
  interval_s: number;
  recovery_delay_s: number;
  max_failures: number;
  seed: number;
  duration_s: number | null;
}

export interface RandomFailureState {
  running: boolean;
  config: RandomFailureConfig | null;
  failures: number;
  recoveries: number;
}

export interface SimulationSnapshot {
  network: NetworkState;
  random_failures: RandomFailureState;
  inflight_operations: number;
}

export interface ClusterSnapshot {
  status: "running" | "stopped";
  health: Health;
  leader_id: string | null;
  term: number;
  active_nodes: number;
  failed_nodes: number;
  total_nodes: number;
  quorum_size: number;
  split_brain: boolean;
  commit_index: number;
  applied_index: number;
  total_log_entries: number;
  throughput: number;
  avg_latency_ms: number;
  p95_latency_ms: number;
  election_ms: number | null;
  uptime_s: number;
  nodes: NodeStatus[];
  simulation: SimulationSnapshot;
  timestamp: number;
}

export interface ClusterEvent {
  seq: number;
  ts: number;
  source: "node" | "network" | "controller" | "client";
  node_id: string | null;
  type: string;
  severity: Severity;
  message: string;
  data: Record<string, unknown>;
  node_seq?: number;
}

export interface MetricsPoint {
  ts: number;
  throughput: number;
  write_latency_avg_ms: number;
  write_latency_p50_ms: number;
  write_latency_p95_ms: number;
  write_latency_p99_ms: number;
  read_latency_avg_ms: number;
  read_latency_p95_ms: number;
  replication_latency_ms: number;
  election_ms: number;
  node_availability_pct: number;
  network_latency_ms: number;
  commit_index: number;
  leader_id: string | null;
  term: number;
  successful_ops: number;
  failed_ops: number;
  active_nodes: number;
}

export interface MetricsPayload {
  current: MetricsPoint | Record<string, never>;
  history: MetricsPoint[];
  election_durations_ms: number[];
  totals: { successful_ops: number; failed_ops: number };
}

export interface KVResult {
  committed: true;
  index: number;
  term: number;
  leader: string;
  result: { op: string; key: string; value?: string } | null;
}

export interface KVListResponse {
  items: Record<string, string>;
  applied_index: number;
  source: string;
  stale: boolean;
  commit_index: number;
}

export interface KVReadResponse {
  key: string;
  value: string | null;
  exists: boolean;
  index: number;
  commit_index: number;
  term: number;
  read_mode: string;
}

export interface LogEntry {
  index: number;
  term: number;
  kind: "command" | "noop";
  command: { op: string; key?: string; value?: string } | null;
}

export interface LogView {
  node_id: string;
  term: number;
  role: Role;
  leader_id: string | null;
  snapshot_index: number;
  commit_index: number;
  applied_index: number;
  last_log_index: number;
  entries: LogEntry[];
  source: string;
  is_leader: boolean;
}

export interface ConsistencyCheck {
  passed: boolean;
  checked: number;
  violations: number;
  skipped: number;
}

export interface ExperimentAssertion {
  name: string;
  passed: boolean;
  detail: string;
}

export interface ExperimentReport {
  id: string;
  kind: string;
  created_at: number;
  finished_at?: number;
  config: {
    scenario: string;
    description?: string;
    duration_s: number;
    rate: number;
    cluster_size?: number;
    initial_leader?: string;
    [key: string]: unknown;
  };
  topology?: {
    nodes: Array<{ node_id: string; http_port: number; proxy_port: number; role: Role; term: number }>;
    leader: string | null;
    term: number;
    cluster_size: number;
    quorum_size: number;
  };
  hardware?: Record<string, string | number>;
  methodology?: string;
  faults?: Record<string, unknown>;
  workload?: {
    duration_s: number;
    measurement_window_s: number;
    target_rate_ops_s: number;
    write_attempts: number;
    write_successes: number;
    write_failures: number;
    read_attempts: number;
    read_successes: number;
    read_failures: number;
    throughput_ops_s: number;
    cluster_commit_throughput_ops_s: number;
    acknowledged_keys: number;
    sample_errors: string[];
    latency: Record<string, number>;
  };
  recovery?: {
    recovery_s: number | null;
    election_ms: number | null;
    fault_window_s: number;
    final_leader: string | null;
    final_term: number | null;
    final_commit_index: number;
    final_applied_index: number;
    cluster_commits: number;
  };
  consistency?: {
    passed: boolean;
    samples: number;
    checks: Record<string, ConsistencyCheck>;
    violations: Array<{ check: string; message: string; ts: number; evidence: Record<string, unknown> }>;
    total_checks_executed: number;
    total_violations: number;
    final_convergence: { converged: boolean; kv_count?: number; applied_indexes?: Record<string, number> };
  };
  final_state?: {
    converged: boolean;
    kv_count: number;
    acknowledged_state_error: string | null;
    applied_indexes: Record<string, number>;
  };
  assertions?: ExperimentAssertion[];
  summary?: {
    throughput_ops_s: number;
    p95_ms: number;
    p99_ms: number;
    write_failures: number;
    recovery_s: number | null;
    consistency: string;
  };
  outcome: "passed" | "failed";
  event_timeline?: ClusterEvent[];
  scenarios?: Array<{
    scenario: string;
    outcome: string;
    throughput_ops_s: number;
    p50_ms: number;
    p95_ms: number;
    p99_ms: number;
    write_failures: number;
    recovery_s: number | null;
    consistency: boolean;
  }>;
  report_ids?: string[];
}

export interface ExperimentSummary {
  id: string;
  created_at: number;
  scenario: string;
  kind: string;
  duration_s: number;
  outcome: string;
  consistency_passed: boolean;
  summary: ExperimentReport["summary"];
}
