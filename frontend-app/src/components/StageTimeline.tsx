import type { CandidateProgress, StageStatus } from "../types";

const LABEL: Record<StageStatus, string> = {
  pending: "Pending",
  running: "Running",
  done: "Done",
  error: "Error",
  skipped: "Skipped",
};

// Text label plus a distinct glyph for every state, so colour is never the only signal.
const GLYPH: Record<StageStatus, string> = {
  pending: "○", // hollow circle
  running: "◔", // circle with upper right quadrant
  done: "✓", // check mark
  error: "✕", // multiplication x
  skipped: "–", // en dash
};

const TONE: Record<StageStatus, string> = {
  pending: "border-line bg-canvas text-muted",
  running: "border-accent bg-accent-soft text-accent-ink",
  done: "border-ok/30 bg-ok-soft text-ok",
  error: "border-stop/30 bg-stop-soft text-stop",
  skipped: "border-line bg-neutral-soft text-muted",
};

const CANDIDATE_LABEL: Record<CandidateProgress["status"], string> = {
  pending: "Waiting",
  running: "In progress",
  done: "Finished",
  error: "Could not be screened",
};

/** One-sentence summary for screen readers, built from real state only. */
export function progressAnnouncement(progress: CandidateProgress[]): string {
  const running = progress.filter((p) => p.status === "running");
  if (running.length === 0) return "";
  return running
    .map((p) => `${p.candidate_name}: ${p.current_stage ? `${p.current_stage} running` : "starting"}`)
    .join(". ");
}

export default function StageTimeline({ progress }: { progress: CandidateProgress[] }) {
  if (progress.length === 0) return null;
  return (
    <div data-testid="stage-timeline">
      <p role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {progressAnnouncement(progress)}
      </p>
      <ul className="mt-3 space-y-3" aria-label="Screening progress for each candidate">
        {progress.map((p) => (
          <li key={p.index} className="rounded-lg border border-line bg-canvas p-3">
            <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
              <span className="font-semibold">{p.candidate_name}</span>
              <span className="text-xs text-muted">{CANDIDATE_LABEL[p.status]}</span>
            </div>
            <ol className="grid grid-cols-2 gap-1.5 sm:grid-cols-3 lg:grid-cols-6" aria-label={`Stages for ${p.candidate_name}`}>
              {p.stages.map((s, i) => (
                <li
                  key={s.name}
                  data-stage={s.name}
                  data-status={s.status}
                  aria-current={s.status === "running" ? "step" : undefined}
                  className={`flex items-center gap-1.5 rounded-lg border px-2 py-1.5 text-[11.5px] ${TONE[s.status]}`}
                >
                  <span aria-hidden className={`w-3 text-center ${s.status === "running" ? "animate-pulse" : ""}`}>{GLYPH[s.status]}</span>
                  <span className="min-w-0">
                    <span className="block font-semibold">{`${i + 1}. ${s.name}`}</span>
                    <span className="block">{LABEL[s.status]}</span>
                  </span>
                </li>
              ))}
            </ol>
          </li>
        ))}
      </ul>
    </div>
  );
}
