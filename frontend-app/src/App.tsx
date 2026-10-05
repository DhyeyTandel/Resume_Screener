import { lazy, Suspense, useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQueries, useQuery } from "@tanstack/react-query";
import { getCandidate, getHealth, getSamples, getScreening, postForm, rowFromReport } from "./api";
import type { Row } from "./types";
import JdPanel from "./components/JdPanel";
import ResumeUpload, { type Consents, type FileEntry, type FileState } from "./components/ResumeUpload";
import ResultsTable from "./components/ResultsTable";
const CandidateDrawer = lazy(() => import("./components/CandidateDrawer"));
import StageTimeline from "./components/StageTimeline";
import { Chip, ErrorBox, Spinner } from "./components/ui";
import { servedSummary } from "./components/provenance";

const FAIRNESS =
  "This is a decision-support tool. A human recruiter must review all recommendations. Do not use protected characteristics or proxies in screening.";

export default function App() {
  const [jd, setJd] = useState("");
  const [entries, setEntries] = useState<FileEntry[]>([]);
  const [paste, setPaste] = useState("");
  const [consents, setConsents] = useState<Consents>({ github: true, linkedin: true, portfolio: true });
  const [screeningId, setScreeningId] = useState<string | null>(null);
  const [view, setView] = useState<"samples" | "screening">("samples");
  const [selected, setSelected] = useState<Row | null>(null);
  const [tried, setTried] = useState(false);

  const health = useQuery({ queryKey: ["health"], queryFn: getHealth });
  const samples = useQuery({ queryKey: ["samples"], queryFn: getSamples });
  const screening = useQuery({
    queryKey: ["screening", screeningId],
    queryFn: () => getScreening(screeningId as string),
    enabled: !!screeningId,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      if (q.state.status === "error" || s === "complete" || s === "interrupted") return false;
      return 700;
    },
  });

  const usable = entries.filter((e) => !e.problem);
  const hasResume = usable.length > 0 || paste.trim() !== "";
  const status = screening.data?.status;
  const submit = useMutation({
    mutationFn: async () => {
      const fd = new FormData();
      fd.append("jd_text", jd);
      usable.forEach((e) => fd.append("files", e.file));
      if (paste.trim()) fd.append("pasted_resumes", paste);
      // Backend aligns these lists by index with `files`, so pad blanks up to the last used slot.
      const pad = (vals: string[], key: string) => {
        let last = -1;
        vals.forEach((v, i) => { if (v) last = i; });
        vals.slice(0, last + 1).forEach((v) => fd.append(key, v));
      };
      pad(usable.map((e) => e.github), "github_usernames");
      pad(usable.map((e) => e.portfolio), "portfolio_urls");
      let lastLi = -1;
      usable.forEach((e, i) => { if (e.linkedin) lastLi = i; });
      usable.slice(0, lastLi + 1).forEach((e) => fd.append("linkedin_files", e.linkedin ?? new File([], "")));
      fd.append("consent_github", String(consents.github));
      fd.append("consent_linkedin", String(consents.linkedin));
      fd.append("consent_portfolio", String(consents.portfolio));
      return postForm<{ screening_id: string }>("/v1/screenings", fd);
    },
    onSuccess: (res) => { setScreeningId(res.screening_id); setView("screening"); },
  });

  const running =
    submit.isPending ||
    (!!screeningId && !screening.isError && status !== "complete" && status !== "interrupted");

  const onScreen = () => {
    setTried(true);
    if (!jd.trim() || !hasResume) return;
    submit.mutate();
  };

  const sampleRows = useMemo(() => (samples.data ?? []).map(rowFromReport), [samples.data]);
  const screenedBase = screening.data?.candidates;
  // Row JSON carries no provenance, so read each report (same cache key as the drawer).
  const reports = useQueries({
    queries: (screenedBase ?? []).map((c) => ({
      queryKey: ["candidate", c.candidate_id],
      queryFn: () => getCandidate(c.candidate_id),
      staleTime: 60_000,
    })),
  });
  const screenedRows: Row[] = useMemo(
    () =>
      (screenedBase ?? []).map((c, i) => {
        const q = reports[i];
        const provenance = q?.data ? (q.data.extensions.meta?.provenance ?? null) : q?.isError ? null : undefined;
        return { ...c, provenance, source: "screening" as const };
      }),
    [screenedBase, reports.map((q) => q.dataUpdatedAt + q.status).join("|")],
  );
  const served = view === "screening" ? servedSummary(screenedRows.map((r) => r.provenance)) : null;
  const rows = view === "samples" ? sampleRows : screenedRows;

  const fileStates = useMemo(() => {
    const out: Record<string, FileState> = {};
    if (!screeningId || !screening.data) {
      if (screeningId) usable.forEach((e) => (out[e.id] = "queued"));
      return out;
    }
    const { done, candidates, status: st } = screening.data;
    usable.forEach((e, i) => {
      if (i < done) out[e.id] = candidates[i]?.status === "Error" ? "error" : "screened";
      else if (i === done && st === "processing") out[e.id] = "screening";
      else out[e.id] = "queued";
    });
    return out;
  }, [screeningId, screening.data, usable]);

  const close = useCallback(() => setSelected(null), []);
  const trigger = useRef<HTMLElement | null>(null);
  const open = useCallback((r: Row) => {
    // Remember the control that opened the drawer before the page goes inert (which blurs it).
    trigger.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setSelected(r);
  }, []);
  const headerRef = useRef<HTMLElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  // While the drawer is open the page behind it is inert: not focusable, not in the
  // accessibility tree. Together with the drawer's own focus trap this keeps Tab inside it.
  // Layout effect: inert must be gone before the drawer's cleanup returns focus to the row button.
  useLayoutEffect(() => {
    const els = [headerRef.current, mainRef.current];
    els.forEach((el) => el && (selected ? el.setAttribute("inert", "") : el.removeAttribute("inert")));
    if (!selected && trigger.current?.isConnected) trigger.current.focus();
    if (!selected) trigger.current = null;
    return () => els.forEach((el) => el?.removeAttribute("inert"));
  }, [selected]);
  const total = screening.data?.total ?? 0;
  const done = screening.data?.done ?? 0;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const jdMissing = tried && !jd.trim();
  const resumeMissing = tried && !hasResume;

  return (
    <div className="min-h-screen">
      <header ref={headerRef} className="sticky top-0 z-30">
        <div className="flex flex-wrap items-center gap-3 border-b border-line bg-surface px-4 py-3 sm:px-6">
          <h1 className="text-[17px] font-bold">AI Resume Screening Assistant</h1>
          {health.isLoading && <Chip tone="mute">Checking provider...</Chip>}
          {health.isError && <Chip tone="stop" title="The /v1/health call failed">Backend unreachable</Chip>}
          {health.data?.mock_mode && <Chip tone="warn" title="No real model is configured. Prose comes from a deterministic template.">Mock analysis mode</Chip>}
          {health.data && !health.data.mock_mode && (
            <Chip
              tone={health.data.key_present === false ? "warn" : "info"}
              title={`Configuration only: this does not prove a model answered. Fallback chain: ${(health.data.fallback_chain ?? []).join(", ") || "unknown"}.`}
            >
              {`Configured: ${health.data.configured_provider ?? health.data.llm_provider}${
                health.data.key_present === undefined ? "" : health.data.key_present ? " (key present)" : " (no key)"
              }`}
            </Chip>
          )}
          {served && <Chip tone="mute" title="What actually wrote the prose in the current screening">{`Served: ${served}`}</Chip>}
        </div>
        <div role="note" className="border-b border-accent/20 bg-accent-soft px-4 py-2 text-xs text-accent-ink sm:px-6">
          <strong>Fairness notice.</strong> {FAIRNESS}
        </div>
      </header>

      <main ref={mainRef} className="mx-auto max-w-6xl space-y-4 px-4 pb-16 pt-5 sm:px-6">
        {view === "samples" && (
          <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-warn/30 bg-warn-soft px-4 py-3 text-warn">
            <span className="font-semibold">Sample data: run your own screening below</span>
            <span className="text-xs">These six candidates are pre-computed examples and are not saved to the database.</span>
          </div>
        )}

        <JdPanel value={jd} onChange={setJd} />
        {jdMissing && <p role="alert" className="-mt-2 text-xs text-stop">Add a job description to enable screening.</p>}

        <ResumeUpload
          entries={entries} onEntries={setEntries} paste={paste} onPaste={setPaste}
          consents={consents} onConsents={setConsents} fileStates={fileStates} disabled={running}
        />

        <section className="card" aria-labelledby="run-h">
          <h2 id="run-h" className="sr-only">Run screening</h2>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" className="btn" disabled={running || !jd.trim() || !hasResume} onClick={onScreen}>
              {running ? "Screening..." : "Screen Candidates"}
            </button>
            {!jd.trim() && <span className="text-xs text-muted">Add a job description to enable this button.</span>}
            {jd.trim() && !hasResume && <span className={`text-xs ${resumeMissing ? "text-stop" : "text-muted"}`}>Upload or paste at least one resume.</span>}
          </div>
          {submit.isError && <div className="mt-3"><ErrorBox message={`${(submit.error as Error).message} ${(submit.error as { remediation?: string }).remediation ?? ""}`.trim()} /></div>}

          {screeningId && (
            <div className="mt-4">
              {screening.isError ? (
                <ErrorBox message={`Progress could not be read: ${(screening.error as Error).message}`} onRetry={() => screening.refetch()} />
              ) : !screening.data ? (
                <Spinner label="Starting screening" />
              ) : (
                <>
                  <div className="mb-1 flex justify-between text-xs text-muted">
                    <span>{`${done} of ${total} candidates screened`}</span><span>{pct}%</span>
                  </div>
                  <div className="h-2 overflow-hidden rounded-full bg-neutral-soft" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} aria-label="Screening progress">
                    <div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} />
                  </div>
                  <StageTimeline progress={screening.data.progress ?? []} />
                  {status === "complete" && <p role="status" className="mt-2 font-medium text-ok">{`Done. ${total} candidate${total === 1 ? "" : "s"} screened. Results are below.`}</p>}
                  {status === "interrupted" && (
                    <p role="alert" className="mt-2 font-medium text-stop">
                      {`The screening was interrupted by a server restart after ${done} of ${total} candidates. Results so far are shown below. Run the screening again for the rest.`}
                    </p>
                  )}
                </>
              )}
            </div>
          )}
        </section>

        <section className="card" aria-labelledby="results-h">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h2 id="results-h" className="text-sm font-semibold">3. Results {view === "samples" ? "(sample data)" : ""}</h2>
            {screeningId && (
              <button type="button" className="btn-ghost" onClick={() => setView(view === "samples" ? "screening" : "samples")}>
                {view === "samples" ? "Show my screening" : "Show sample data"}
              </button>
            )}
          </div>
          {view === "samples" && samples.isLoading ? <Spinner label="Loading sample data" />
            : view === "samples" && samples.isError ? <ErrorBox message={`Sample data could not be loaded: ${(samples.error as Error).message}`} onRetry={() => samples.refetch()} />
            : view === "screening" && screening.isLoading ? <Spinner label="Loading results" />
            : <ResultsTable rows={rows} onOpen={open} emptyMessage={view === "samples" ? "No sample data is available." : "No candidates yet. Results appear here as each resume is screened."} />}
        </section>
      </main>

      {selected && (
        <Suspense fallback={<div className="fixed inset-0 z-40 bg-ink/45"><Spinner label="Loading details" /></div>}>
          <CandidateDrawer row={selected} sampleReports={samples.data ?? []} onClose={close} />
        </Suspense>
      )}
    </div>
  );
}
