import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api } from "../services/api";
import type { ClusterEvent, ClusterSnapshot, MetricsPayload } from "../types";

interface ClusterContextValue {
  cluster: ClusterSnapshot | null;
  events: ClusterEvent[];
  metrics: MetricsPayload | null;
  connected: boolean;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

const ClusterContext = createContext<ClusterContextValue | null>(null);

function mergeEvents(existing: ClusterEvent[], incoming: ClusterEvent[]): ClusterEvent[] {
  const bySeq = new Map<number, ClusterEvent>();
  for (const event of existing) bySeq.set(event.seq, event);
  for (const event of incoming) bySeq.set(event.seq, event);
  return Array.from(bySeq.values())
    .sort((left, right) => left.seq - right.seq)
    .slice(-1500);
}

function socketUrl(): string {
  const configured = import.meta.env.VITE_WS_URL as string | undefined;
  if (configured) return configured;
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/ws/cluster`;
}

export function ClusterProvider({ children }: { children: ReactNode }) {
  const [cluster, setCluster] = useState<ClusterSnapshot | null>(null);
  const [events, setEvents] = useState<ClusterEvent[]>([]);
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const connectedRef = useRef(false);
  const mountedRef = useRef(true);
  const retryRef = useRef<number | null>(null);
  const socketRef = useRef<WebSocket | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [snapshot, metricPayload, eventPayload] = await Promise.all([
        api.getCluster(),
        api.metrics(),
        api.events(500),
      ]);
      if (!mountedRef.current) return;
      setCluster(snapshot);
      setMetrics(metricPayload);
      setEvents((current) => mergeEvents(current, eventPayload.events));
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : "Unable to reach controller");
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void refresh();

    const interval = window.setInterval(() => {
      void api
        .metrics()
        .then((payload) => mountedRef.current && setMetrics(payload))
        .catch(() => undefined);
      if (!connectedRef.current) {
        void api
          .getCluster()
          .then((snapshot) => {
            if (!mountedRef.current) return;
            setCluster(snapshot);
            setError(null);
          })
          .catch((err: unknown) => {
            if (mountedRef.current) {
              setError(err instanceof Error ? err.message : "Controller unreachable");
            }
          });
      }
    }, 2000);

    const connect = () => {
      if (!mountedRef.current) return;
      try {
        const socket = new WebSocket(socketUrl());
        socketRef.current = socket;
        socket.onopen = () => {
          if (!mountedRef.current) return;
          connectedRef.current = true;
          setConnected(true);
          setError(null);
        };
        socket.onmessage = (message) => {
          if (!mountedRef.current) return;
          try {
            const payload = JSON.parse(message.data as string) as {
              type: string;
              data?: ClusterSnapshot;
              event?: ClusterEvent;
            };
            if (payload.type === "state" && payload.data) {
              setCluster(payload.data);
            } else if (payload.type === "event" && payload.event) {
              setEvents((current) => mergeEvents(current, [payload.event as ClusterEvent]));
            }
          } catch {
            /* ignore malformed frame */
          }
        };
        socket.onerror = () => socket.close();
        socket.onclose = () => {
          if (!mountedRef.current) return;
          connectedRef.current = false;
          setConnected(false);
          retryRef.current = window.setTimeout(connect, 1800);
        };
      } catch {
        retryRef.current = window.setTimeout(connect, 2500);
      }
    };
    connect();

    return () => {
      mountedRef.current = false;
      window.clearInterval(interval);
      if (retryRef.current) window.clearTimeout(retryRef.current);
      socketRef.current?.close();
    };
  }, [refresh]);

  const value = useMemo(
    () => ({ cluster, events, metrics, connected, loading, error, refresh }),
    [cluster, events, metrics, connected, loading, error, refresh],
  );

  return <ClusterContext.Provider value={value}>{children}</ClusterContext.Provider>;
}

export function useCluster(): ClusterContextValue {
  const value = useContext(ClusterContext);
  if (!value) throw new Error("useCluster must be used inside ClusterProvider");
  return value;
}
