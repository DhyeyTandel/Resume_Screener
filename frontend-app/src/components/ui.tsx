import type { ReactNode } from "react";

// Full class names spelled out on purpose: Tailwind drops @layer classes it cannot
// find literally in the source, so a template like `chip-${tone}` compiled to nothing
// for every tone except the one that happened to appear elsewhere (chip-mute).
const TONE_CLASS = {
  ok: "chip-ok",
  warn: "chip-warn",
  stop: "chip-stop",
  mute: "chip-mute",
  info: "chip-info",
} as const;

export function Chip({ tone, children, title }: { tone: keyof typeof TONE_CLASS; children: ReactNode; title?: string }) {
  return <span title={title} className={`chip ${TONE_CLASS[tone]}`}>{children}</span>;
}

export function recTone(r: string): "ok" | "warn" | "stop" | "mute" {
  return r === "Shortlist" ? "ok" : r === "Review Manually" ? "warn" : r === "Not Recommended" ? "stop" : "mute";
}

export function RecChip({ rec }: { rec: string }) {
  return <Chip tone={recTone(rec)}>Suggested: {rec}</Chip>;
}

const BAND_LABEL: Record<string, string> = {
  HIGH_TRUST: "High trust",
  MODERATE: "Moderate trust",
  NEEDS_VERIFICATION: "Needs verification",
  INSUFFICIENT_EVIDENCE: "Insufficient evidence",
};
const BAND_TONE: Record<string, "ok" | "warn" | "stop" | "mute"> = {
  HIGH_TRUST: "ok",
  MODERATE: "warn",
  NEEDS_VERIFICATION: "stop",
};

export function BandChip({ band }: { band?: string | null }) {
  if (!band) return <Chip tone="mute">Not assessed</Chip>;
  return <Chip tone={BAND_TONE[band] ?? "mute"}>{BAND_LABEL[band] ?? band}</Chip>;
}

export function IntegrityChip({ action }: { action?: string }) {
  if (!action || action === "proceed") return <Chip tone="ok">No issues</Chip>;
  return <Chip tone="stop">{`Flagged: ${action.replace(/_/g, " ")}`}</Chip>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-muted">{children}</p>;
}

export function Spinner({ label }: { label: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-2 py-8 text-muted">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-line border-t-accent" aria-hidden />
      {label}
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-lg border border-stop/30 bg-stop-soft p-3 text-stop">
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="btn-ghost mt-2" onClick={onRetry}>Try again</button>
      )}
    </div>
  );
}
