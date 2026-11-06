import { useEffect } from "react";
import { AlertTriangle, X } from "lucide-react";
import { Spinner } from "./ui";
import { classNames } from "../utils/format";

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  tone = "danger",
  busy = false,
  onConfirm,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "danger" | "primary";
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children?: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const listener = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", listener);
    return () => window.removeEventListener("keydown", listener);
  }, [open, busy, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-[2px]"
        onClick={() => !busy && onClose()}
        aria-hidden
      />
      <div className="relative w-full max-w-md animate-fade-in rounded-lg border border-console-600 bg-console-900 shadow-2xl">
        <div className="flex items-start justify-between gap-4 px-5 pt-5">
          <div className="flex items-start gap-3">
            <div
              className={classNames(
                "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-full border",
                tone === "danger"
                  ? "border-signal-red/30 bg-signal-red/10 text-signal-red"
                  : "border-accent-500/30 bg-accent-500/10 text-accent-300",
              )}
            >
              <AlertTriangle size={15} />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-console-100">{title}</h2>
              {description ? (
                <p className="mt-1 text-xs leading-relaxed text-console-400">{description}</p>
              ) : null}
            </div>
          </div>
          <button
            type="button"
            className="text-console-500 hover:text-console-200"
            onClick={onClose}
            disabled={busy}
            aria-label="Close dialog"
          >
            <X size={16} />
          </button>
        </div>
        {children ? <div className="px-5 py-3">{children}</div> : null}
        <div className="flex justify-end gap-2 border-t border-console-700/60 px-5 py-3">
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={busy}>
            {cancelLabel}
          </button>
          <button
            type="button"
            className={classNames("btn", tone === "danger" ? "btn-danger" : "btn-primary")}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? <Spinner /> : null}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
