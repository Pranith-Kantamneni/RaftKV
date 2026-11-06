import type { ReactNode } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Crown,
  Loader2,
  ShieldAlert,
  ShieldCheck,
  UserRound,
  Vote,
} from "lucide-react";
import { classNames, formatNumber, severityRank } from "../utils/format";
import type { Health, Role, Severity } from "../types";

export function StatusDot({
  tone = "neutral",
  pulse = false,
  className,
}: {
  tone?: "green" | "amber" | "red" | "cyan" | "neutral" | "violet";
  pulse?: boolean;
  className?: string;
}) {
  const colors: Record<string, string> = {
    green: "bg-signal-green",
    amber: "bg-signal-amber",
    red: "bg-signal-red",
    cyan: "bg-accent-500",
    neutral: "bg-console-500",
    violet: "bg-signal-violet",
  };
  return (
    <span
      className={classNames(
        "inline-block h-1.5 w-1.5 rounded-full",
        colors[tone],
        pulse && "animate-pulse-dot",
        className,
      )}
    />
  );
}

export function RoleBadge({ role }: { role: Role | string }) {
  const config: Record<string, { className: string; icon: ReactNode }> = {
    leader: { className: "chip-cyan", icon: <Crown size={10} /> },
    follower: { className: "chip-neutral", icon: <UserRound size={10} /> },
    candidate: { className: "chip-amber", icon: <Vote size={10} /> },
    offline: { className: "chip-red", icon: <CircleDashed size={10} /> },
  };
  const item = config[role] ?? config.offline;
  return (
    <span className={classNames("chip", item.className)}>
      {item.icon}
      {role}
    </span>
  );
}

export function HealthBadge({ health }: { health: Health }) {
  const config: Record<Health, { className: string; label: string; icon: ReactNode }> = {
    healthy: {
      className: "chip-green",
      label: "Healthy",
      icon: <ShieldCheck size={11} />,
    },
    degraded: {
      className: "chip-amber",
      label: "Degraded",
      icon: <AlertTriangle size={11} />,
    },
    critical: {
      className: "chip-red",
      label: "Critical",
      icon: <ShieldAlert size={11} />,
    },
    stopped: {
      className: "chip-neutral",
      label: "Stopped",
      icon: <CircleDashed size={11} />,
    },
  };
  const item = config[health];
  return (
    <span className={classNames("chip", item.className)}>
      {item.icon}
      {item.label}
    </span>
  );
}

export function SeverityBadge({ severity }: { severity: Severity | string }) {
  const map: Record<string, string> = {
    debug: "chip-neutral",
    info: "chip-cyan",
    warning: "chip-amber",
    error: "chip-red",
    critical: "chip-red",
  };
  return (
    <span className={classNames("chip", map[severity] ?? "chip-neutral")}>{severity}</span>
  );
}

export function StatCard({
  label,
  value,
  detail,
  icon,
  tone = "neutral",
  spark,
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  icon?: ReactNode;
  tone?: "neutral" | "green" | "amber" | "red" | "cyan";
  spark?: number[];
}) {
  const toneClass = {
    neutral: "text-console-100",
    green: "text-signal-green",
    amber: "text-signal-amber",
    red: "text-signal-red",
    cyan: "text-accent-300",
  }[tone];
  return (
    <div className="stat-card">
      <div className="flex items-start justify-between gap-2">
        <span className="stat-label">{label}</span>
        {icon ? <span className="text-console-500">{icon}</span> : null}
      </div>
      <div className={classNames("stat-value", toneClass)}>{value}</div>
      <div className="mt-1 flex items-end justify-between gap-2">
        <span className="text-[11px] text-console-400">{detail}</span>
        {spark && spark.length > 1 ? (
          <Sparkline values={spark} className={toneClass} width={72} height={22} />
        ) : null}
      </div>
    </div>
  );
}

export function Sparkline({
  values,
  width = 120,
  height = 32,
  className = "text-accent-400",
}: {
  values: number[];
  width?: number;
  height?: number;
  className?: string;
}) {
  const clean = values.filter((value) => Number.isFinite(value));
  if (clean.length < 2) return <span className="text-[10px] text-console-500">no data</span>;
  const min = Math.min(...clean);
  const max = Math.max(...clean);
  const span = max - min || 1;
  const points = clean
    .map((value, index) => {
      const x = (index / (clean.length - 1)) * width;
      const y = height - 2 - ((value - min) / span) * (height - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className={className}>
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}

export function InlineAlert({
  tone = "info",
  title,
  children,
  action,
}: {
  tone?: "info" | "warning" | "error" | "success";
  title?: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  const config = {
    info: "border-console-600 bg-console-850 text-console-200",
    warning: "border-signal-amber/30 bg-signal-amber/5 text-signal-amber",
    error: "border-signal-red/30 bg-signal-red/5 text-signal-red",
    success: "border-signal-green/30 bg-signal-green/5 text-signal-green",
  }[tone];
  const icon =
    tone === "error" ? (
      <ShieldAlert size={15} />
    ) : tone === "warning" ? (
      <AlertTriangle size={15} />
    ) : tone === "success" ? (
      <CheckCircle2 size={15} />
    ) : (
      <CircleDashed size={15} />
    );
  return (
    <div className={classNames("flex items-start gap-3 rounded-md border px-3 py-2.5 text-xs", config)}>
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0 flex-1">
        {title ? <div className="font-semibold">{title}</div> : null}
        {children ? <div className="mt-0.5 text-[11px] opacity-90">{children}</div> : null}
      </div>
      {action}
    </div>
  );
}

export function EmptyState({
  title,
  description,
  icon,
  action,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex min-h-[140px] flex-col items-center justify-center gap-2 px-6 py-8 text-center">
      <div className="text-console-500">{icon ?? <CircleDashed size={22} />}</div>
      <div className="text-sm font-medium text-console-200">{title}</div>
      {description ? <div className="max-w-sm text-xs text-console-400">{description}</div> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 size={14} className={classNames("animate-spin", className)} />;
}

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-console-100">{title}</h1>
        {description ? (
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-console-400">{description}</p>
        ) : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export function SectionHeading({
  title,
  meta,
  actions,
}: {
  title: string;
  meta?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <div className="flex items-baseline gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-console-300">{title}</h2>
        {meta ? <span className="text-[10px] text-console-500">{meta}</span> : null}
      </div>
      {actions}
    </div>
  );
}

export function DataCell({
  label,
  value,
  className,
}: {
  label: string;
  value: ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <div className="text-[10px] uppercase tracking-wider text-console-500">{label}</div>
      <div className="mt-0.5 font-mono text-xs text-console-200">{value}</div>
    </div>
  );
}

export function severityOrder(severity: string): number {
  return severityRank(severity);
}

export function formatCounter(value: number | undefined): string {
  return formatNumber(value ?? 0);
}
