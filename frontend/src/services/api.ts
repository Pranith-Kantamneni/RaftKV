import type {
  ClusterEvent,
  ClusterSnapshot,
  ExperimentReport,
  ExperimentSummary,
  KVListResponse,
  KVReadResponse,
  KVResult,
  LogView,
  MetricsPayload,
  SimulationSnapshot,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch (error) {
    throw new ApiError(0, error instanceof Error ? error.message : "network error");
  }
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : typeof payload === "string"
          ? payload
          : `request failed (${response.status})`;
    throw new ApiError(response.status, detail);
  }
  return payload as T;
}

export const api = {
  // cluster
  getCluster: () => request<ClusterSnapshot>("/api/cluster"),
  startCluster: () => request<ClusterSnapshot>("/api/cluster/start", { method: "POST" }),
  stopCluster: () => request<ClusterSnapshot>("/api/cluster/stop", { method: "POST" }),
  crashNode: (nodeId: string) =>
    request<ClusterSnapshot>(`/api/nodes/${nodeId}/crash`, { method: "POST" }),
  restartNode: (nodeId: string) =>
    request<ClusterSnapshot>(`/api/nodes/${nodeId}/restart`, { method: "POST" }),
  nodeLogs: (nodeId: string, lines = 80) =>
    request<{ node_id: string; log: string }>(`/api/nodes/${nodeId}/logs?lines=${lines}`),
  leaderFailure: () =>
    request<{ crashed: string; cluster: ClusterSnapshot }>("/api/simulation/leader-failure", {
      method: "POST",
    }),

  // key-value
  kvSet: (key: string, value: string) =>
    request<KVResult>("/api/kv", {
      method: "POST",
      body: JSON.stringify({ op: "set", key, value }),
    }),
  kvDelete: (key: string) => request<KVResult>(`/api/kv/${encodeURIComponent(key)}`, { method: "DELETE" }),
  kvGet: (key: string) => request<KVReadResponse>(`/api/kv/${encodeURIComponent(key)}`),
  kvList: () => request<KVListResponse>("/api/kv"),
  log: (limit = 200) => request<LogView>(`/api/log?limit=${limit}`),

  // simulation
  getSimulation: () => request<SimulationSnapshot>("/api/simulation"),
  partition: (groups: string[][]) =>
    request<ClusterSnapshot>("/api/simulation/partition", {
      method: "POST",
      body: JSON.stringify({ groups }),
    }),
  heal: () => request<ClusterSnapshot>("/api/simulation/heal", { method: "POST" }),
  setLatency: (latencyMs: number, source?: string, target?: string) =>
    request<ClusterSnapshot>("/api/simulation/latency", {
      method: "POST",
      body: JSON.stringify({ latency_ms: latencyMs, source, target }),
    }),
  setPacketLoss: (loss: number, source?: string, target?: string) =>
    request<ClusterSnapshot>("/api/simulation/packet-loss", {
      method: "POST",
      body: JSON.stringify({ loss, source, target }),
    }),
  startRandomFailures: (config: {
    probability: number;
    interval_s: number;
    recovery_delay_s: number;
    max_failures: number;
    seed: number;
    duration_s?: number | null;
  }) =>
    request<ClusterSnapshot>("/api/simulation/random-failures", {
      method: "POST",
      body: JSON.stringify(config),
    }),
  stopRandomFailures: () =>
    request<ClusterSnapshot>("/api/simulation/stop", { method: "POST" }),
  resetSimulation: () =>
    request<ClusterSnapshot>("/api/simulation/reset", { method: "POST" }),

  // monitoring
  metrics: () => request<MetricsPayload>("/api/metrics"),
  events: (limit = 500) => request<{ events: ClusterEvent[] }>(`/api/events?limit=${limit}`),

  // experiments
  experiments: () => request<{ experiments: ExperimentSummary[] }>("/api/experiments"),
  experiment: (id: string) => request<ExperimentReport>(`/api/experiments/${id}`),
  runExperiment: (scenario: string, durationS = 12, rate = 6) =>
    request<ExperimentReport>("/api/experiments/run", {
      method: "POST",
      body: JSON.stringify({ scenario, duration_s: durationS, rate }),
    }),
  runBenchmark: (durationS = 10, rate = 6) =>
    request<ExperimentReport>("/api/benchmarks/run", {
      method: "POST",
      body: JSON.stringify({ duration_s: durationS, rate }),
    }),
};
