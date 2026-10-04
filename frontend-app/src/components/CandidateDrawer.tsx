import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { getCandidate } from "../api";
import type { Report, Row } from "../types";
import { ErrorBox, RecChip, Spinner } from "./ui";
import { AuditTab, AuthenticityTab, IntegrityTab, OverviewTab, QuestionsTab, RequirementsTab, SkillsTab } from "./tabs";

const TABS = [
  ["overview", "Overview"],
  ["requirements", "Requirements"],
  ["skills", "Skill Intelligence"],
  ["authenticity", "Authenticity"],
  ["integrity", "Integrity"],
  ["questions", "Interview Questions"],
  ["audit", "Audit"],
] as const;
type TabId = (typeof TABS)[number][0];

interface Props {
  row: Row;
  /** Full report for sample candidates (they are not in the database). */
  sampleReports: Report[];
  onClose: () => void;
}

export default function CandidateDrawer({ row, sampleReports, onClose }: Props) {
  const isSample = row.source === "sample";
  const [tab, setTab] = useState<TabId>("overview");
  const panel = useRef<HTMLDivElement>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  const remote = useQuery({
    queryKey: ["candidate", row.candidate_id],
    queryFn: () => getCandidate(row.candidate_id),
    enabled: !isSample,
  });
  const report: Report | undefined = isSample
    ? sampleReports.find((r) => r.extensions.candidate_id === row.candidate_id)
    : remote.data;

  // Escape closes; focus moves into the drawer and returns on close; focus stays inside.
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); onClose(); return; }
      if (e.key === "Tab" && panel.current) {
        const f = panel.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (f.length === 0) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      if (prev && prev !== document.body) prev.focus?.();
    };
  }, [onClose]);

  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex(([id]) => id === tab);
    let n = i;
    if (e.key === "ArrowRight") n = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") n = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") n = 0;
    else if (e.key === "End") n = TABS.length - 1;
    else return;
    e.preventDefault();
    setTab(TABS[n][0]);
    tabRefs.current[TABS[n][0]]?.focus();
  };

  const body = () => {
    if (!isSample && remote.isLoading) return <Spinner label="Loading candidate report" />;
    if (!isSample && remote.isError) return <ErrorBox message={(remote.error as Error).message} onRetry={() => remote.refetch()} />;
    if (!report) return <ErrorBox message="This candidate report is not available." />;
    switch (tab) {
      case "overview": return <OverviewTab r={report} />;
      case "requirements": return <RequirementsTab r={report} />;
      case "skills": return <SkillsTab r={report} />;
      case "authenticity": return <AuthenticityTab r={report} />;
      case "integrity": return <IntegrityTab r={report} />;
      case "questions": return <QuestionsTab r={report} />;
      case "audit": return <AuditTab id={row.candidate_id} isSample={isSample} />;
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-ink/45" onClick={onClose} aria-hidden />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        tabIndex={-1}
        className="relative flex h-full w-full max-w-3xl flex-col bg-surface shadow-drawer outline-none"
      >
        <div className="flex items-start justify-between gap-3 border-b border-line px-5 py-4">
          <div>
            <h2 id="drawer-title" className="text-base font-bold">{row.candidate_name}</h2>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
              <span>Score {Math.round(row.overall_match_score)}</span>
              <RecChip rec={row.recommendation} />
              {isSample && <span className="chip chip-mute">Sample data</span>}
            </div>
          </div>
          <button type="button" className="btn-ghost" onClick={onClose}>Close</button>
        </div>
        <div role="tablist" aria-label="Candidate detail sections" onKeyDown={onTabKey} className="flex gap-1 overflow-x-auto border-b border-line px-3 pt-2">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              ref={(el) => { tabRefs.current[id] = el; }}
              role="tab"
              id={`tab-${id}`}
              type="button"
              aria-selected={tab === id}
              aria-controls="tabpanel"
              tabIndex={tab === id ? 0 : -1}
              onClick={() => setTab(id)}
              className={`whitespace-nowrap rounded-t-lg px-3 py-2 text-[12.5px] font-semibold ${tab === id ? "bg-accent-soft text-accent" : "text-muted hover:text-ink"}`}
            >
              {label}
            </button>
          ))}
        </div>
        <div id="tabpanel" role="tabpanel" aria-labelledby={`tab-${tab}`} tabIndex={0} className="flex-1 overflow-y-auto px-5 py-4">
          {body()}
        </div>
        <div className="border-t border-line bg-canvas px-5 py-2 text-[11.5px] text-muted">
          {report?.fairness_notice ?? "This is a decision-support tool. A human recruiter must review all recommendations."}{" "}
          Provider: {report?.extensions.meta?.llm_provider ?? "unknown"}{report?.extensions.meta?.mock_mode ? " (mock analysis mode)" : ""}.
        </div>
      </div>
    </div>
  );
}
