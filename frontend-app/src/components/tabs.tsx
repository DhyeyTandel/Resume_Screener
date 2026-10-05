import { useState, type FormEvent, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { getAudit, recordDecision } from "../api";
import type { Report } from "../types";
import { ProvenanceList } from "./provenance";
import { BandChip, Chip, Empty, ErrorBox, Spinner } from "./ui";

const H3 = ({ children }: { children: ReactNode }) => <h3 className="h3">{children}</h3>;
const List = ({ items, none }: { items?: string[]; none: string }) =>
  items && items.length ? (
    <ul className="list-disc space-y-1 pl-5">{items.map((x, i) => <li key={i}>{x}</li>)}</ul>
  ) : (
    <p className="text-muted">{none}</p>
  );
const KV = ({ k, children }: { k: string; children: ReactNode }) => (
  <>
    <dt className="text-muted">{k}</dt>
    <dd className="m-0">{children}</dd>
  </>
);

/* ---------- Overview ---------- */
export function OverviewTab({ r }: { r: Report }) {
  const b = r.extensions.score_breakdown ?? {};
  const rows = (b.per_requirement_contribution ?? []).filter((c) => !c.excluded).map((c) => ({
    name: c.requirement.length > 24 ? c.requirement.slice(0, 22) + "..." : c.requirement,
    full: c.requirement,
    Earned: Number(c.contribution.toFixed(2)),
    "Possible": c.weight,
  }));
  return (
    <div>
      <p>{r.summary}</p>
      <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1.5 text-[13px] sm:grid-cols-[180px_1fr]">
        <KV k="Base score">{b.base_score ?? "-"}</KV>
        <KV k="Integrity penalty">{`x${b.integrity_penalty ?? 1}`}</KV>
        <KV k="Overall (shown)"><strong>{Math.round(r.overall_match_score)}</strong></KV>
        <KV k="Score confidence">
          {b.score_confidence ?? "-"} <span className="text-xs text-muted">(share of requirements that could be judged)</span>
        </KV>
        <KV k="AI-writing indicators">
          <Chip tone="mute">{r.ai_text_indicators?.level ?? "n/a"}</Chip>
          <div className="text-xs text-muted">{r.ai_text_indicators?.disclaimer} It has no weight in the score.</div>
        </KV>
      </dl>

      <H3>Why this score</H3>
      {rows.length === 0 ? (
        <Empty>No score breakdown is available for this candidate.</Empty>
      ) : (
        <figure aria-label="Score breakdown by requirement" className="m-0">
          <div style={{ height: Math.max(220, rows.length * 34 + 60) }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={rows} layout="vertical" margin={{ left: 8, right: 16, top: 4, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#e3e6ea" />
                <XAxis type="number" tick={{ fontSize: 11 }} />
                <YAxis type="category" dataKey="name" width={150} tick={{ fontSize: 11 }} />
                <Tooltip formatter={(v) => String(v)} labelFormatter={(_, p) => p?.[0]?.payload?.full ?? ""} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="Possible" fill="#d3ddf5" radius={[0, 3, 3, 0]} />
                <Bar dataKey="Earned" fill="#2d5bd7" radius={[0, 3, 3, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
          <figcaption className="sr-only">
            {rows.map((x) => `${x.full}: ${x.Earned} of ${x.Possible}`).join(". ")}
          </figcaption>
        </figure>
      )}

      <H3>Why this recommendation</H3>
      <List items={r.extensions.recommendation_reasons} none="No recommendation reasons were recorded." />
      <H3>Strengths</H3>
      <List items={r.strengths} none="None listed." />
      <H3>Risks</H3>
      <List items={r.risks} none="None listed." />
      <H3>Which model wrote this</H3>
      <ProvenanceList entries={r.extensions.meta?.provenance} />
    </div>
  );
}

/* ---------- Requirements ---------- */
const REQ_STYLE: Record<string, { box: string; tone: "ok" | "warn" | "stop" | "info" | "mute"; hint?: string }> = {
  Matched: { box: "border-l-4 border-l-ok", tone: "ok" },
  "Partially Matched": { box: "border-l-4 border-l-accent", tone: "info" },
  Missing: { box: "border-l-4 border-l-stop bg-stop-soft/40", tone: "stop", hint: "Nothing in the resume supports this requirement." },
  "Not Enough Evidence": {
    box: "border-l-4 border-dashed border-l-warn bg-warn-soft/40",
    tone: "warn",
    hint: "The resume is silent or too thin to judge. This is not the same as missing, so ask about it.",
  },
};

export function RequirementsTab({ r }: { r: Report }) {
  if (!r.requirement_match?.length) return <Empty>No requirements were extracted.</Empty>;
  return (
    <div>
      <p className="mb-3 flex flex-wrap items-center gap-2 text-xs text-muted">
        Legend: <Chip tone="stop">Missing</Chip> no support found
        <Chip tone="warn">Not Enough Evidence</Chip> too little to judge
      </p>
      <ul className="space-y-2">
        {r.requirement_match.map((q, i) => {
          const s = REQ_STYLE[q.status] ?? { box: "border-l-4 border-l-line", tone: "mute" as const };
          return (
            <li key={i} className={`rounded-lg border border-line p-3 ${s.box}`}>
              <div className="flex flex-wrap items-center gap-2">
                <strong>{q.requirement}</strong>
                <Chip tone="mute">{q.priority}</Chip>
                <Chip tone={s.tone}>{q.status === "Missing" ? "Missing" : q.status === "Not Enough Evidence" ? "? Not Enough Evidence" : q.status}</Chip>
              </div>
              {q.resume_evidence && <p className="mt-2 text-xs text-muted">Evidence: &ldquo;{q.resume_evidence}&rdquo;</p>}
              {q.notes && <p className="mt-1 text-xs text-muted">{q.notes}</p>}
              {s.hint && <p className="mt-1 text-xs font-medium">{s.hint}</p>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/* ---------- Skill Intelligence ---------- */
export function SkillsTab({ r }: { r: Report }) {
  const items = r.extensions.skill_intelligence ?? [];
  if (!items.length) return <Empty>Skill intelligence is not available for this candidate.</Empty>;
  const tone = (c: string) => (c === "Exact Match" ? "ok" : c === "No Evidence" ? "stop" : "warn");
  return (
    <ul className="space-y-2">
      {items.map((s, i) => {
        const pct = Math.round(s.semantic_match * 100);
        return (
          <li key={i} className="rounded-lg border border-line p-3">
            <div className="flex flex-wrap items-center gap-2">
              <strong>{s.required_skill}</strong>
              <Chip tone={tone(s.classification)}>{s.classification}</Chip>
              <Chip tone="mute">{s.semantic_level}</Chip>
            </div>
            <div className="mt-2 flex items-center gap-2 text-xs text-muted">
              <span>Semantic match</span>
              <div className="h-2 w-40 overflow-hidden rounded-full bg-neutral-soft" role="img" aria-label={`Semantic match ${pct} percent`}>
                <div className="h-full bg-accent" style={{ width: `${pct}%` }} />
              </div>
              <span>{pct}%</span>
            </div>
            <p className="mt-2">{s.recruiter_suggestion}</p>
            <p className="mt-1 text-xs text-muted">{s.reasoning}</p>
            <dl className="mt-2 grid gap-x-3 gap-y-1 text-xs sm:grid-cols-[150px_1fr]">
              <KV k="Supporting skills">{s.supporting_skills.join(", ") || "none"}</KV>
              <KV k="Supporting projects">{s.supporting_projects.join(", ") || "none"}</KV>
              <KV k="Missing concepts">{s.missing_concepts.join(", ") || "none"}</KV>
            </dl>
          </li>
        );
      })}
    </ul>
  );
}

/* ---------- Authenticity ---------- */
const CLAIM_TONE: Record<string, "ok" | "warn" | "stop" | "mute"> = {
  VERIFIED: "ok", SUPPORTED: "ok", WEAK: "warn", UNSUPPORTED: "stop", CONTRADICTED: "stop", UNVERIFIABLE: "mute",
};
const SRC_TONE: Record<string, "ok" | "warn" | "stop" | "mute"> = { ok: "ok", missing: "mute", no_consent: "warn", error: "stop" };

export function AuthenticityTab({ r }: { r: Report }) {
  const a = r.extensions.authenticity;
  if (!a) return <Empty>An authenticity assessment is not available for this candidate.</Empty>;
  const insufficient = a.band === "INSUFFICIENT_EVIDENCE";
  return (
    <div>
      <p>{a.recruiter_summary}</p>
      <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1.5 text-[13px] sm:grid-cols-[180px_1fr]">
        <KV k="Band">
          <BandChip band={a.band} />
          {insufficient && <span className="ml-2 text-xs text-muted">No headline number is shown because the sources are too thin.</span>}
        </KV>
        <KV k="Assessment confidence">{a.scores?.assessment_confidence ?? "-"}</KV>
        <KV k="Coverage">{a.scores?.coverage ?? "-"}</KV>
        <KV k="Sources">
          <span className="flex flex-wrap gap-1.5">
            {Object.entries(a.sources_used ?? {}).map(([k, v]) => (
              <Chip key={k} tone={SRC_TONE[v] ?? "mute"}>{`${k}: ${v.replace("_", " ")}`}</Chip>
            ))}
          </span>
        </KV>
      </dl>

      <H3>{`Claims (${a.claims?.length ?? 0})`}</H3>
      {a.claims?.length ? (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[520px] text-[13px]">
            <thead>
              <tr className="border-b border-line text-left text-[11px] uppercase tracking-wide text-muted">
                <th scope="col" className="py-1.5 pr-2">Type</th>
                <th scope="col" className="py-1.5 pr-2">Claim</th>
                <th scope="col" className="py-1.5 pr-2">Status</th>
                <th scope="col" className="py-1.5">Citations</th>
              </tr>
            </thead>
            <tbody>
              {a.claims.map((c) => (
                <tr key={c.claim_id} className="border-b border-line align-top">
                  <td className="py-2 pr-2 text-xs text-muted">{c.type}</td>
                  <td className="py-2 pr-2">
                    {c.text}
                    {c.rationale && <div className="text-xs text-muted">{c.rationale}</div>}
                  </td>
                  <td className="py-2 pr-2"><Chip tone={CLAIM_TONE[c.status] ?? "mute"}>{c.status}</Chip></td>
                  <td className="py-2 text-xs">
                    {c.evidence?.length ? (
                      <ul className="space-y-1">
                        {c.evidence.map((e, i) => (
                          <li key={i}><code className="rounded bg-neutral-soft px-1">{e.source}</code> {e.citation}{e.note ? <span className="text-muted">{` (${e.note})`}</span> : null}</li>
                        ))}
                      </ul>
                    ) : <span className="text-muted">No citation</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="text-muted">No claims were extracted.</p>}

      <H3>Contradictions</H3>
      {a.contradictions?.length ? (
        <ul className="space-y-1">
          {a.contradictions.map((c, i) => (
            <li key={i} className="rounded-lg border border-stop/30 bg-stop-soft/50 p-2">
              <Chip tone="stop">{c.type.replace(/_/g, " ")}</Chip> {c.detail}
              {c.sources?.length ? <span className="text-xs text-muted">{` (sources: ${c.sources.join(", ")})`}</span> : null}
            </li>
          ))}
        </ul>
      ) : <p className="text-muted">No contradictions found.</p>}

      <H3>Verification gaps</H3>
      {a.verification_gaps?.length ? (
        <ul className="space-y-1">
          {a.verification_gaps.map((g, i) => (
            <li key={i}>{g.what_to_verify} <Chip tone={g.priority === "high" ? "warn" : "mute"}>{g.priority}</Chip></li>
          ))}
        </ul>
      ) : <p className="text-muted">No verification gaps.</p>}

      <H3>Flags</H3>
      {a.authenticity_flags?.length ? (
        <ul className="space-y-1">
          {a.authenticity_flags.map((f, i) => (
            <li key={i}><Chip tone="warn">{f.flag.replace(/_/g, " ")}</Chip> {f.detail}</li>
          ))}
        </ul>
      ) : <p className="text-muted">No flags.</p>}
    </div>
  );
}

/* ---------- Integrity ---------- */
export function IntegrityTab({ r }: { r: Report }) {
  const i = r.extensions.integrity;
  if (!i) return <Empty>No integrity result is available for this candidate.</Empty>;
  const adj = i.score_adjustment ?? {};
  const clean = !i.recommended_action || i.recommended_action === "proceed";
  return (
    <div>
      <p className="font-semibold">{i.headline || "No integrity findings."}</p>
      <dl className="mt-3 grid grid-cols-1 gap-x-4 gap-y-1.5 text-[13px] sm:grid-cols-[180px_1fr]">
        <KV k="Intent"><Chip tone={i.intent === "benign" ? "ok" : i.intent === "deliberate" ? "stop" : "warn"}>{i.intent ?? "unknown"}</Chip></KV>
        <KV k="Recommended action"><Chip tone={clean ? "ok" : "stop"}>{(i.recommended_action ?? "proceed").replace(/_/g, " ")}</Chip></KV>
        <KV k="Base to adjusted">{adj.base_score ?? "-"} x {adj.penalty ?? 1} = <strong>{adj.adjusted_score ?? "-"}</strong></KV>
      </dl>
      <H3>Findings</H3>
      {i.findings?.length ? (
        <ul className="space-y-2">
          {i.findings.map((f, k) => (
            <li key={k} className="rounded-lg border border-line border-l-4 border-l-stop p-3">
              <strong>{f.code.replace(/_/g, " ")}</strong>
              <p>{f.plain_explanation}</p>
              {f.quoted_evidence && (
                <p className="my-1 rounded bg-canvas px-2 py-1 font-mono text-xs text-ink" data-testid="quoted-evidence">
                  {f.quoted_evidence}
                </p>
              )}
              <p className="text-xs text-muted">Why it matters: {f.why_it_matters}</p>
              <p className="text-xs text-muted">Possible innocent explanation: {f.benign_alternative}</p>
            </li>
          ))}
        </ul>
      ) : <p className="text-muted">No findings. The document passed its integrity check.</p>}
      {i.audit_log_entry && <p className="mt-3 text-xs text-muted">Audit line: {i.audit_log_entry}</p>}
      <p className="mt-3 rounded-lg bg-accent-soft p-3 text-accent-ink" role="note">
        {i.human_decides || "A human recruiter always makes the final call."}
      </p>
    </div>
  );
}

/* ---------- Interview questions ---------- */
export function QuestionsTab({ r }: { r: Report }) {
  const d = r.extensions.interview_questions_detailed;
  const qs = d?.interview_questions ?? [];
  if (!qs.length) return <Empty>No interview questions were generated.</Empty>;
  const prio = new Map(r.requirement_match.map((m) => [m.requirement.toLowerCase(), m.priority]));
  const rank = (skill: string) => {
    const p = prio.get(skill.toLowerCase());
    return p === "Must Have" ? 0 : p === "Preferred" ? 1 : 2;
  };
  const sorted = [...qs].sort((a, b) => rank(a.skill) - rank(b.skill));
  const groups: [string, typeof qs][] = [
    ["Must-have requirements", sorted.filter((q) => rank(q.skill) === 0)],
    ["Preferred requirements", sorted.filter((q) => rank(q.skill) === 1)],
    ["Other topics", sorted.filter((q) => rank(q.skill) === 2)],
  ];
  return (
    <div>
      {groups.filter(([, g]) => g.length).map(([title, g]) => (
        <section key={title}>
          <H3>{title}</H3>
          <ul className="space-y-2">
            {g.map((q, i) => (
              <li key={i} className="rounded-lg border border-line border-l-4 border-l-accent p-3">
                <div className="flex flex-wrap items-center gap-2"><strong>{q.skill}</strong><Chip tone="mute">{q.evidence_level}</Chip></div>
                <p className="my-1.5">{q.question}</p>
                <p className="text-xs text-muted">Purpose: {q.purpose}</p>
                <p className="text-xs text-muted">Risk if unanswered: {q.risk_if_unanswered}</p>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {d?.skipped_strong_evidence?.length ? (
        <p className="mt-3 text-xs text-muted">Skipped because already strongly evidenced: {d.skipped_strong_evidence.join(", ")}.</p>
      ) : null}
    </div>
  );
}

/* ---------- Audit ---------- */
export function AuditTab({ id, isSample }: { id: string; isSample: boolean }) {
  const qc = useQueryClient();
  const [decision, setDecision] = useState("Advance to interview");
  const [note, setNote] = useState("");
  const log = useQuery({ queryKey: ["audit", id], queryFn: () => getAudit(id), enabled: !isSample });
  const save = useMutation({
    mutationFn: () => recordDecision(id, decision, note),
    onSuccess: () => { setNote(""); qc.invalidateQueries({ queryKey: ["audit", id] }); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };

  return (
    <div>
      {isSample && (
        <p role="note" className="mb-3 rounded-lg bg-warn-soft p-3 text-warn">
          This is sample data and is not stored in the database, so decisions cannot be recorded. Run your own screening to use the audit log.
        </p>
      )}
      <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
        <div>
          <label htmlFor="decision" className="block text-xs text-muted">Recruiter decision</label>
          <select id="decision" className="input" value={decision} disabled={isSample || save.isPending} onChange={(e) => setDecision(e.target.value)}>
            <option>Advance to interview</option>
            <option>Hold</option>
            <option>Decline</option>
          </select>
        </div>
        <div className="min-w-[200px] flex-1">
          <label htmlFor="note" className="block text-xs text-muted">Note (optional)</label>
          <input id="note" className="input" value={note} disabled={isSample || save.isPending} onChange={(e) => setNote(e.target.value)} />
        </div>
        <button type="submit" className="btn" disabled={isSample || save.isPending}>{save.isPending ? "Saving..." : "Record decision"}</button>
      </form>
      {save.isError && <div className="mt-2"><ErrorBox message={(save.error as Error).message} /></div>}
      <p className="mt-2 text-xs text-muted">Decisions are appended to an append-only audit log with a timestamp and config version. The tool only suggests. You decide.</p>

      <H3>Audit log</H3>
      {isSample ? <Empty>No audit log for sample candidates.</Empty>
        : log.isLoading ? <Spinner label="Loading audit log" />
        : log.isError ? <ErrorBox message={(log.error as Error).message} onRetry={() => log.refetch()} />
        : !log.data?.length ? <Empty>No decisions have been recorded yet.</Empty>
        : (
          <ul className="space-y-2">
            {[...log.data].reverse().map((a, i) => (
              <li key={i} className="rounded-lg border border-line p-3">
                <strong>{a.decision}</strong> <span className="text-xs text-muted">{new Date(a.at).toLocaleString()}</span>
                {a.config_version && <span className="text-xs text-muted">{` config ${a.config_version}`}</span>}
                {a.note && <p className="text-xs text-muted">{a.note}</p>}
              </li>
            ))}
          </ul>
        )}
    </div>
  );
}
