import type { ProvenanceEntry } from "../types";
import { Chip } from "./ui";

const TASK_LABEL: Record<string, string> = {
  narrative: "Narrative summary",
  integrity_interpret: "Integrity explanation",
  claim_judge: "Claim judge",
  interview_questions: "Interview questions",
};
const PRETTY: Record<string, string> = { openrouter: "OpenRouter", anthropic: "Anthropic", ollama: "Ollama" };
const pretty = (p: string) => PRETTY[p] ?? p;

/** "nvidia/nemotron-3-super-120b-a12b:free" becomes "nemotron-3-super-120b-a12b". */
export function shortModel(model?: string | null): string {
  if (!model) return "unknown model";
  return model.split("/").pop()!.split(":")[0];
}

/** A short human reason from a sanitized error string such as "openrouter: HTTP 429". */
export function shortReason(reason?: string | null): string {
  if (!reason) return "";
  const first = reason.split(";")[0];
  const prov = /^(\w+):/.exec(first)?.[1];
  const who = prov && prov !== "template" ? pretty(prov) : "the model";
  if (/HTTP 429|rate.?limit/i.test(first)) return `${who} rate-limited`;
  if (/no API key/i.test(first)) return `${who} has no API key`;
  if (/unreachable/i.test(first)) return `${who} unreachable`;
  if (/timed out/i.test(first)) return `${who} timed out`;
  const http = /HTTP (\d{3})/.exec(first);
  if (http) return `${who} HTTP ${http[1]}`;
  if (/lacked the required keys/i.test(reason)) return "model output was unusable";
  return first.length > 60 ? `${first.slice(0, 57)}...` : first;
}

export interface ProseLabel {
  text: string;
  tone: "ok" | "warn" | "mute";
  title: string;
}

/** What wrote the narrative prose. Mock and template output is always named as such. */
export function proseLabel(entries?: ProvenanceEntry[] | null): ProseLabel {
  if (entries === undefined) return { text: "Prose: checking...", tone: "mute", title: "Loading the report's provenance" };
  const n = entries?.find((e) => e.task === "narrative");
  if (!n) {
    return { text: "Prose: not recorded", tone: "mute", title: "This report predates provenance tracking, so the model that wrote it is unknown." };
  }
  const why = shortReason(n.reason);
  const title = `${n.provider} / ${n.model ?? "n/a"}${n.reason ? `. ${n.reason}` : ""}`;
  if (n.provider === "template") return { text: `Prose: template${why ? ` (${why})` : ""}`, tone: "warn", title };
  if (n.provider === "mock") {
    return { text: `Prose: mock template (${n.fell_back && why ? why : "no model configured"})`, tone: "warn", title };
  }
  return {
    text: `Prose: ${n.provider} / ${shortModel(n.model)}${n.fell_back ? ` (fallback${why ? `: ${why}` : ""})` : ""}`,
    tone: n.fell_back ? "warn" : "ok",
    title,
  };
}

export function ProseChip({ entries }: { entries?: ProvenanceEntry[] | null }) {
  const l = proseLabel(entries);
  return <Chip tone={l.tone} title={l.title}>{l.text}</Chip>;
}

function served(e: ProvenanceEntry): string {
  if (e.provider === "template") return "Deterministic template (no model text used)";
  if (e.provider === "mock") return "Mock template (deterministic placeholder, not a model)";
  return `${pretty(e.provider)} / ${e.model ?? "unknown model"}`;
}

/** Detail list for the drawer: one row per LLM-using task. */
export function ProvenanceList({ entries }: { entries?: ProvenanceEntry[] }) {
  if (!entries || entries.length === 0) {
    return <p className="text-muted">Not recorded. This report predates provenance tracking, so which model wrote it is unknown.</p>;
  }
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-1.5 text-[13px] sm:grid-cols-[180px_1fr]">
      {entries.map((e) => (
        <div key={e.task} className="contents" data-testid="provenance-row">
          <dt className="text-muted">{TASK_LABEL[e.task] ?? e.task}</dt>
          <dd className="m-0">
            {served(e)}
            <span className="text-xs text-muted">{` - ${e.calls} call${e.calls === 1 ? "" : "s"}`}</span>
            {e.fell_back && (
              <div className="text-xs text-warn">{`Fell back${e.reason ? `: ${e.reason}` : ""}`}</div>
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Counts of who wrote the prose across a set of rows, e.g. "mock template x2, openrouter / nemotron x1". */
export function servedSummary(all: (ProvenanceEntry[] | null | undefined)[]): string | null {
  const counts = new Map<string, number>();
  for (const entries of all) {
    const n = entries?.find((e) => e.task === "narrative");
    if (!n) continue;
    const key = n.provider === "mock" || n.provider === "template" ? "mock template" : `${n.provider} / ${shortModel(n.model)}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  if (counts.size === 0) return null;
  return [...counts].map(([k, v]) => `${k} x${v}`).join(", ");
}
