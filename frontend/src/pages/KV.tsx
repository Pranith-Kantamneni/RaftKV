import { useEffect, useMemo, useState } from "react";
import {
  Database,
  Eraser,
  KeyRound,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Server,
  Trash2,
  Upload,
} from "lucide-react";
import { useCluster } from "../hooks/useCluster";
import { api } from "../services/api";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState, InlineAlert, PageHeader, SectionHeading, Spinner, StatusDot } from "../components/ui";
import { classNames, formatNumber } from "../utils/format";
import type { KVListResponse, KVReadResponse, KVResult } from "../types";

export function KVPage() {
  const { cluster, refresh } = useCluster();
  const [listing, setListing] = useState<KVListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState(false);
  const [readResult, setReadResult] = useState<KVReadResponse | null>(null);
  const [lastWrite, setLastWrite] = useState<KVResult | null>(null);
  const [deleteKey, setDeleteKey] = useState<string | null>(null);

  const load = async (showSpinner = true) => {
    if (showSpinner) setLoading(true);
    try {
      const payload = await api.kvList();
      setListing(payload);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not list keys");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const entries = useMemo(() => {
    const items = Object.entries(listing?.items ?? {});
    const needle = query.trim().toLowerCase();
    return items
      .filter(([itemKey, itemValue]) => !needle || itemKey.toLowerCase().includes(needle) || itemValue.toLowerCase().includes(needle))
      .sort(([left], [right]) => left.localeCompare(right));
  }, [listing, query]);

  const totalBytes = useMemo(
    () => Object.values(listing?.items ?? {}).reduce((sum, item) => sum + new Blob([item]).size, 0),
    [listing],
  );

  const write = async () => {
    const trimmedKey = key.trim();
    if (!trimmedKey) {
      setError("Key must not be blank.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.kvSet(trimmedKey, value);
      setLastWrite(result);
      setNotice(`SET ${trimmedKey} committed at index ${result.index} (term ${result.term}).`);
      setEditing(false);
      setValue("");
      setKey("");
      await load(false);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Write failed");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!deleteKey) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await api.kvDelete(deleteKey);
      setLastWrite(result);
      setReadResult(null);
      setNotice(`DELETE ${deleteKey} committed at index ${result.index}.`);
      setDeleteKey(null);
      await load(false);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Delete failed");
    } finally {
      setBusy(false);
    }
  };

  const read = async (target: string) => {
    setBusy(true);
    setError(null);
    try {
      const result = await api.kvGet(target);
      setReadResult(result);
      setNotice(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Read failed");
    } finally {
      setBusy(false);
    }
  };

  const editEntry = (itemKey: string, itemValue: string) => {
    setKey(itemKey);
    setValue(itemValue);
    setEditing(true);
    setReadResult(null);
  };

  const leader = cluster?.leader_id ?? null;

  return (
    <div>
      <PageHeader
        title="Key-value store"
        description="Client operations are sent to the controller, which forwards them only to the current Raft leader. A write response means the entry was committed by a majority and applied locally."
        actions={
          <>
            <button type="button" className="btn btn-ghost" onClick={() => void load()} disabled={loading}>
              <RefreshCw size={13} className={loading ? "animate-spin" : ""} /> Refresh
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => {
                setKey("");
                setValue("");
                setEditing(true);
                setReadResult(null);
              }}
            >
              <Plus size={13} /> New key
            </button>
          </>
        }
      />

      {error ? <div className="mb-3"><InlineAlert tone="error" title="Operation failed">{error}</InlineAlert></div> : null}
      {notice ? <div className="mb-3"><InlineAlert tone="success" title="Committed">{notice}</InlineAlert></div> : null}

      <div className="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <KVStat label="Leader" value={leader ?? "none"} detail={leader ? `term ${cluster?.term ?? "—"}` : "writes cannot commit"} tone={leader ? "cyan" : "red"} icon={<Server size={13} />} />
        <KVStat label="Keys" value={formatNumber(entries.length)} detail={`${formatNumber(Object.keys(listing?.items ?? {}).length)} total in snapshot`} icon={<KeyRound size={13} />} />
        <KVStat label="Applied index" value={formatNumber(listing?.applied_index)} detail={`commit ${formatNumber(listing?.commit_index)}`} icon={<Database size={13} />} />
        <KVStat label="Payload" value={`${(totalBytes / 1024).toFixed(1)} KB`} detail={listing?.stale ? "listing may be stale" : `source ${listing?.source ?? "—"}`} icon={<Upload size={13} />} tone={listing?.stale ? "amber" : "neutral"} />
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="panel overflow-hidden">
          <div className="panel-header">
            <SectionHeading title="Keys" meta={listing ? `applied ${formatNumber(listing.applied_index)}` : "loading"} />
            <div className="relative w-full max-w-[240px]">
              <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-console-500" />
              <input
                className="input py-1.5 pl-7 text-xs"
                placeholder="Filter keys or values…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                aria-label="Filter keys"
              />
            </div>
          </div>
          {loading && !listing ? (
            <div className="flex min-h-[260px] items-center justify-center gap-2 text-xs text-console-400">
              <Spinner /> Reading state machine…
            </div>
          ) : entries.length ? (
            <div className="max-h-[620px] overflow-auto">
              <table className="data-table">
                <thead className="sticky top-0 z-10 bg-console-900">
                  <tr>
                    <th>Key</th>
                    <th>Value</th>
                    <th className="w-24">Size</th>
                    <th className="w-32">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map(([itemKey, itemValue]) => (
                    <tr key={itemKey}>
                      <td className="align-top">
                        <div className="font-mono text-[11px] font-medium text-console-100">{itemKey}</div>
                      </td>
                      <td className="max-w-[520px] align-top">
                        <span className="line-clamp-2 break-all font-mono text-[10px] text-console-400">{itemValue || <em className="text-console-600">empty string</em>}</span>
                      </td>
                      <td className="align-top font-mono text-[10px] text-console-500">{new Blob([itemValue]).size} B</td>
                      <td className="align-top">
                        <div className="flex gap-1">
                          <button type="button" className="btn btn-ghost btn-xs" onClick={() => void read(itemKey)} title="Read through leader">
                            <Database size={10} />
                          </button>
                          <button type="button" className="btn btn-ghost btn-xs" onClick={() => editEntry(itemKey, itemValue)} title="Edit value">
                            <Pencil size={10} />
                          </button>
                          <button type="button" className="btn btn-danger btn-xs" onClick={() => setDeleteKey(itemKey)} title="Delete key">
                            <Trash2 size={10} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              icon={<Database size={22} />}
              title={query ? "No matching keys" : "The state machine is empty"}
              description={query ? "Clear the filter to see all keys." : "Write the first key to commit an entry through Raft."}
            />
          )}
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-console-700/60 px-4 py-2 font-mono text-[10px] text-console-500">
            <span>
              source {listing?.source ?? "—"} · applied {formatNumber(listing?.applied_index)} · commit {formatNumber(listing?.commit_index)}
            </span>
            {listing?.stale ? <span className="text-signal-amber">stale read — leader unavailable</span> : null}
          </div>
        </div>

        <div className="space-y-4">
          <div className="panel">
            <div className="panel-header">
              <span className="panel-title">{editing ? "Write key" : "Key operation"}</span>
              {editing ? (
                <button type="button" className="text-[10px] text-console-500 hover:text-console-200" onClick={() => { setEditing(false); setKey(""); setValue(""); }}>
                  cancel
                </button>
              ) : null}
            </div>
            <div className="space-y-3 p-4">
              <div>
                <label className="label" htmlFor="kv-key">Key</label>
                <input
                  id="kv-key"
                  className="input font-mono text-xs"
                  placeholder="user:1001"
                  value={key}
                  disabled={!editing || busy}
                  onChange={(event) => setKey(event.target.value)}
                  maxLength={256}
                />
              </div>
              <div>
                <label className="label" htmlFor="kv-value">Value</label>
                <textarea
                  id="kv-value"
                  className="input min-h-[120px] resize-y font-mono text-xs"
                  placeholder="Any UTF-8 value"
                  value={value}
                  disabled={!editing || busy}
                  onChange={(event) => setValue(event.target.value)}
                />
                <div className="mt-1 flex justify-between text-[10px] text-console-500">
                  <span>{new Blob([value]).size} bytes</span>
                  <button type="button" className="flex items-center gap-1 hover:text-console-300" onClick={() => setValue("")} disabled={!editing}>
                    <Eraser size={10} /> clear
                  </button>
                </div>
              </div>
              <button type="button" className="btn btn-primary w-full" disabled={!editing || busy || !key.trim()} onClick={() => void write()}>
                {busy ? <Spinner /> : <Upload size={12} />} Commit SET
              </button>
              <div className="text-[10px] leading-relaxed text-console-500">
                The client receives the committed log index and term. The controller never
                acknowledges a write from a local cache.
              </div>
            </div>
          </div>

          {readResult ? (
            <div className="panel">
              <div className="panel-header">
                <span className="panel-title">Leader read result</span>
                <span className={classNames("chip", readResult.exists ? "chip-green" : "chip-neutral")}>
                  {readResult.exists ? "exists" : "missing"}
                </span>
              </div>
              <div className="space-y-2 p-4">
                <div className="break-all font-mono text-xs text-console-100">{readResult.key}</div>
                <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded border border-console-700 bg-console-950 p-2.5 font-mono text-[10px] text-console-300">
                  {readResult.value ?? "null"}
                </pre>
                <div className="grid grid-cols-2 gap-2 font-mono text-[10px] text-console-400">
                  <span>read mode <span className="text-console-200">{readResult.read_mode}</span></span>
                  <span>index <span className="text-console-200">{readResult.index}</span></span>
                  <span>commit <span className="text-console-200">{readResult.commit_index}</span></span>
                  <span>term <span className="text-console-200">{readResult.term}</span></span>
                </div>
                <button type="button" className="btn btn-ghost w-full" onClick={() => setReadResult(null)}>Dismiss</button>
              </div>
            </div>
          ) : null}

          {lastWrite ? (
            <div className="panel p-4">
              <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-signal-green">
                <StatusDot tone="green" /> Last commit
              </div>
              <div className="space-y-1.5 font-mono text-[10px] text-console-400">
                <div className="flex justify-between"><span>operation</span><span className="text-console-200">{lastWrite.result?.op?.toUpperCase() ?? "—"}</span></div>
                <div className="flex justify-between"><span>key</span><span className="text-console-200">{lastWrite.result?.key ?? "—"}</span></div>
                <div className="flex justify-between"><span>index</span><span className="text-console-200">{lastWrite.index}</span></div>
                <div className="flex justify-between"><span>term</span><span className="text-console-200">{lastWrite.term}</span></div>
                <div className="flex justify-between"><span>leader</span><span className="text-console-200">{lastWrite.leader}</span></div>
              </div>
            </div>
          ) : null}
        </div>
      </div>

      <ConfirmDialog
        open={deleteKey !== null}
        title={`Delete ${deleteKey ?? ""}?`}
        description="A delete is appended as a Raft command, replicated to a majority and applied to every state machine. It cannot be undone from the UI."
        confirmLabel="Delete key"
        tone="danger"
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => !busy && setDeleteKey(null)}
      />
    </div>
  );
}

function KVStat({
  label,
  value,
  detail,
  icon,
  tone = "neutral",
}: {
  label: string;
  value: string;
  detail: string;
  icon: React.ReactNode;
  tone?: "neutral" | "cyan" | "red" | "amber";
}) {
  const colors = {
    neutral: "text-console-100",
    cyan: "text-accent-300",
    red: "text-signal-red",
    amber: "text-signal-amber",
  };
  return (
    <div className="panel px-3 py-3">
      <div className="flex items-center justify-between">
        <span className="text-[9px] uppercase tracking-[0.14em] text-console-500">{label}</span>
        <span className="text-console-600">{icon}</span>
      </div>
      <div className={classNames("mt-1 font-mono text-lg font-semibold", colors[tone])}>{value}</div>
      <div className="mt-0.5 truncate text-[10px] text-console-500">{detail}</div>
    </div>
  );
}
