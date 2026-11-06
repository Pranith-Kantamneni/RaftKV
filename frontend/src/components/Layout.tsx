import { useEffect, useMemo, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  Database,
  FileText,
  FlaskConical,
  LayoutDashboard,
  LineChart,
  ListTree,
  Menu,
  Network,
  RefreshCw,
  Server,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useCluster } from "../hooks/useCluster";
import { classNames, formatDuration } from "../utils/format";
import { HealthBadge, StatusDot } from "./ui";

const NAV_GROUPS = [
  {
    label: "Observe",
    items: [
      { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
      { to: "/cluster", label: "Live cluster", icon: Network },
      { to: "/timeline", label: "Event timeline", icon: ListTree },
      { to: "/metrics", label: "Metrics", icon: LineChart },
    ],
  },
  {
    label: "Operate",
    items: [
      { to: "/simulator", label: "Failure simulator", icon: FlaskConical },
      { to: "/kv", label: "Key-value store", icon: Database },
    ],
  },
  {
    label: "Analyze",
    items: [{ to: "/reports", label: "Experiment reports", icon: FileText }],
  },
];

const TITLES: Record<string, string> = {
  "/": "Cluster overview",
  "/cluster": "Live cluster",
  "/simulator": "Failure simulator",
  "/kv": "Key-value store",
  "/timeline": "Event timeline",
  "/metrics": "Metrics & benchmarks",
  "/reports": "Experiment reports",
};

function Clock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <span className="hidden font-mono text-[11px] tabular-nums text-console-400 sm:block">
      {now.toLocaleTimeString(undefined, { hour12: false })}
    </span>
  );
}

export function Layout() {
  const { cluster, connected, error, refresh } = useCluster();
  const location = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    setMobileOpen(false);
  }, [location.pathname]);

  const activeFaults = useMemo(() => {
    if (!cluster) return 0;
    const network = cluster.simulation.network;
    return (
      (network.partitions.length > 0 ? 1 : 0) +
      (network.default_latency_ms > 0 ? 1 : 0) +
      (network.default_loss > 0 ? 1 : 0) +
      network.links.filter((link) => link.latency_ms > 0 || link.loss > 0).length +
      (cluster.simulation.random_failures.running ? 1 : 0) +
      cluster.failed_nodes
    );
  }, [cluster]);

  const onRefresh = async () => {
    setRefreshing(true);
    await refresh();
    window.setTimeout(() => setRefreshing(false), 400);
  };

  const sidebar = (
    <aside className="flex h-full w-[228px] shrink-0 flex-col border-r border-console-800 bg-console-900/60">
      <div className="flex h-14 items-center gap-2.5 border-b border-console-800 px-4">
        <div className="flex h-7 w-7 items-center justify-center rounded border border-accent-500/30 bg-accent-500/10 text-accent-300">
          <Activity size={15} />
        </div>
        <div>
          <div className="font-mono text-[13px] font-semibold tracking-tight text-console-100">
            RAFT<span className="text-accent-400">//</span>KV
          </div>
          <div className="text-[9px] uppercase tracking-[0.2em] text-console-500">
            consensus console
          </div>
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto px-2 py-3">
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className="mb-4">
            <div className="mb-1 px-2 text-[9px] font-semibold uppercase tracking-[0.2em] text-console-500">
              {group.label}
            </div>
            {group.items.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    classNames(
                      "mb-0.5 flex items-center gap-2.5 rounded-md border-l-2 px-2.5 py-2 text-xs transition-colors",
                      isActive
                        ? "border-accent-500 bg-accent-500/[0.08] font-medium text-console-100"
                        : "border-transparent text-console-400 hover:bg-console-850 hover:text-console-200",
                    )
                  }
                >
                  <Icon size={14} className="shrink-0" />
                  <span className="flex-1">{item.label}</span>
                  {item.to === "/simulator" && activeFaults > 0 ? (
                    <span className="rounded-full border border-signal-amber/30 bg-signal-amber/10 px-1.5 py-0.5 font-mono text-[9px] text-signal-amber">
                      {activeFaults}
                    </span>
                  ) : null}
                </NavLink>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="border-t border-console-800 px-3 py-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[9px] uppercase tracking-[0.18em] text-console-500">Cluster</span>
          {cluster ? <HealthBadge health={cluster.health} /> : null}
        </div>
        <div className="space-y-1 font-mono text-[10px] text-console-400">
          <div className="flex justify-between">
            <span>leader</span>
            <span className="text-console-200">{cluster?.leader_id ?? "none"}</span>
          </div>
          <div className="flex justify-between">
            <span>term</span>
            <span className="text-console-200">{cluster?.term ?? 0}</span>
          </div>
          <div className="flex justify-between">
            <span>quorum</span>
            <span className="text-console-200">
              {cluster ? `${cluster.active_nodes}/${cluster.total_nodes}` : "—"}
            </span>
          </div>
          <div className="flex justify-between">
            <span>uptime</span>
            <span className="text-console-200">{cluster ? formatDuration(cluster.uptime_s) : "—"}</span>
          </div>
        </div>
        <div className="mt-3 flex items-center gap-1.5 text-[10px]">
          <StatusDot tone={cluster?.health === "healthy" ? "green" : cluster?.health === "stopped" ? "neutral" : "amber"} pulse={connected} />
          <span className="text-console-500">
            {cluster?.status === "running" ? "controller online" : "cluster stopped"}
          </span>
        </div>
      </div>
    </aside>
  );

  return (
    <div className="flex h-screen overflow-hidden bg-console-950">
      <div className="hidden lg:flex">{sidebar}</div>
      {mobileOpen ? (
        <div className="fixed inset-0 z-40 flex lg:hidden">
          <div className="absolute inset-0 bg-black/70" onClick={() => setMobileOpen(false)} />
          <div className="relative h-full">{sidebar}</div>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-console-800 bg-console-900/40 px-4 lg:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <button
              type="button"
              className="text-console-400 hover:text-console-100 lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation"
            >
              <Menu size={18} />
            </button>
            <div className="flex items-center gap-2">
              <Server size={14} className="text-console-500" />
              <span className="text-sm font-medium text-console-100">
                {TITLES[location.pathname] ?? "Raft console"}
              </span>
            </div>
            <span className="hidden font-mono text-[10px] text-console-600 xl:block">
              raft-kv / {cluster?.total_nodes ?? 3} nodes / fixed membership
            </span>
          </div>
          <div className="flex items-center gap-2 sm:gap-3">
            {cluster ? (
              <div className="hidden items-center gap-2 rounded-md border border-console-700 bg-console-900 px-2.5 py-1 md:flex">
                <StatusDot
                  tone={
                    cluster.health === "healthy"
                      ? "green"
                      : cluster.health === "critical" || cluster.health === "stopped"
                        ? "red"
                        : "amber"
                  }
                  pulse={cluster.status === "running"}
                />
                <span className="font-mono text-[10px] text-console-300">
                  leader <span className="text-console-100">{cluster.leader_id ?? "—"}</span>
                </span>
                <span className="h-3 w-px bg-console-700" />
                <span className="font-mono text-[10px] text-console-300">
                  term <span className="text-console-100">{cluster.term}</span>
                </span>
                <span className="h-3 w-px bg-console-700" />
                <span className="font-mono text-[10px] text-console-300">
                  q <span className="text-console-100">{cluster.active_nodes}/{cluster.total_nodes}</span>
                </span>
              </div>
            ) : null}
            <Clock />
            <div
              className={classNames(
                "flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-[10px]",
                connected
                  ? "border-signal-green/25 bg-signal-green/5 text-signal-green"
                  : "border-signal-amber/25 bg-signal-amber/5 text-signal-amber",
              )}
              title={connected ? "WebSocket connected" : "WebSocket reconnecting"}
            >
              {connected ? <Wifi size={11} /> : <WifiOff size={11} />}
              {connected ? "live" : "retrying"}
            </div>
            <button
              type="button"
              className="btn btn-ghost px-2 py-1.5"
              onClick={onRefresh}
              disabled={refreshing}
              title="Refresh state"
            >
              <RefreshCw size={13} className={refreshing ? "animate-spin" : ""} />
            </button>
          </div>
        </header>

        {error ? (
          <div className="flex items-center justify-between gap-3 border-b border-signal-red/20 bg-signal-red/[0.06] px-4 py-1.5 text-[11px] text-signal-red lg:px-6">
            <span className="flex items-center gap-2">
              <WifiOff size={12} />
              {error}
            </span>
            <button type="button" className="text-signal-red/80 hover:text-signal-red" onClick={onRefresh}>
              retry
            </button>
          </div>
        ) : null}

        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1600px] px-4 py-5 lg:px-6 lg:py-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
