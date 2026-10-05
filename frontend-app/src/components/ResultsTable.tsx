import { useMemo, useState } from "react";
import type { Row } from "../types";
import { BandChip, Chip, Empty, IntegrityChip, RecChip } from "./ui";
import { ProseChip } from "./provenance";

type SortKey = "candidate_name" | "overall_match_score" | "recommendation" | "status";
const FILTERS = [
  ["All", "All"],
  ["Shortlist", "Shortlist"],
  ["Review Manually", "Review"],
  ["Not Recommended", "Not Recommended"],
] as const;

interface Props {
  rows: Row[];
  onOpen: (r: Row) => void;
  emptyMessage: string;
}

export default function ResultsTable({ rows, onOpen, emptyMessage }: Props) {
  const [filter, setFilter] = useState<string>("All");
  const [q, setQ] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("overall_match_score");
  const [dir, setDir] = useState<1 | -1>(-1);

  const list = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const out = rows.filter(
      (r) =>
        (filter === "All" || r.recommendation === filter) &&
        (!needle ||
          [r.candidate_name, r.recommendation, r.status, ...r.strengths, ...r.gaps].join(" ").toLowerCase().includes(needle)),
    );
    out.sort((a, b) => {
      const x = a[sortKey], y = b[sortKey];
      return (x > y ? 1 : x < y ? -1 : 0) * dir;
    });
    return out;
  }, [rows, filter, q, sortKey, dir]);

  const sortBy = (k: SortKey) => {
    if (k === sortKey) setDir((d) => (d === 1 ? -1 : 1));
    else { setSortKey(k); setDir(k === "candidate_name" ? 1 : -1); }
  };
  const th = (k: SortKey, label: string) => (
    <th scope="col" aria-sort={sortKey === k ? (dir === 1 ? "ascending" : "descending") : "none"} className="px-2 py-2 text-left">
      <button type="button" onClick={() => sortBy(k)} className="text-[11px] font-semibold uppercase tracking-wide text-muted hover:text-ink">
        {label} {sortKey === k ? (dir === 1 ? "▲" : "▼") : ""}
      </button>
    </th>
  );
  const plain = (label: string, cls = "") => (
    <th scope="col" className={`px-2 py-2 text-left text-[11px] font-semibold uppercase tracking-wide text-muted ${cls}`}>{label}</th>
  );

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <label htmlFor="search" className="sr-only">Search candidates</label>
        <input id="search" type="search" className="input !w-56" placeholder="Search candidates" value={q} onChange={(e) => setQ(e.target.value)} />
        <div role="group" aria-label="Filter by recommendation" className="flex flex-wrap gap-1">
          {FILTERS.map(([value, label]) => (
            <button key={value} type="button" aria-pressed={filter === value} onClick={() => setFilter(value)}
              className={`rounded-lg px-3 py-1.5 text-xs font-semibold ${filter === value ? "bg-accent text-white" : "bg-neutral-soft text-muted hover:bg-line"}`}>
              {label}
            </button>
          ))}
        </div>
        <span className="ml-auto text-xs text-muted" aria-live="polite">{list.length} of {rows.length} shown</span>
      </div>

      {rows.length === 0 ? (
        <Empty>{emptyMessage}</Empty>
      ) : list.length === 0 ? (
        <Empty>No candidates match this search or filter.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[980px] border-collapse text-[13px]">
            <thead className="border-b border-line">
              <tr>
                {th("candidate_name", "Candidate")}
                {th("overall_match_score", "Score")}
                {th("recommendation", "Suggested")}
                {plain("Key strengths")}
                {plain("Critical gaps")}
                {th("status", "Status")}
                {plain("Integrity")}
                {plain("Authenticity")}
                {plain("Prose source")}
              </tr>
            </thead>
            <tbody>
              {list.map((r) => {
                const penalised = r.base_score != null && Math.round(r.base_score) !== Math.round(r.overall_match_score);
                return (
                  <tr key={`${r.source}-${r.candidate_id}`} className="cursor-pointer border-b border-line align-top hover:bg-canvas" onClick={() => onOpen(r)}>
                    <td className="px-2 py-3">
                      <button type="button" className="text-left font-semibold text-accent hover:underline"
                        onClick={(e) => { e.stopPropagation(); onOpen(r); }}
                        aria-label={`Open details for ${r.candidate_name}`}>
                        {r.candidate_name}
                      </button>
                      {r.source === "sample" && <div className="text-[11px] text-muted">Sample</div>}
                    </td>
                    <td className="px-2 py-3">
                      <div className="text-[15px] font-bold">{Math.round(r.overall_match_score)}</div>
                      {penalised && <div className="text-[11px] text-muted">base {Math.round(r.base_score as number)}, penalised</div>}
                    </td>
                    <td className="px-2 py-3"><RecChip rec={r.recommendation} /></td>
                    <td className="min-w-[200px] max-w-[260px] px-2 py-3 text-xs text-muted">
                      {r.strengths.length ? (
                        <ul className="space-y-0.5">{r.strengths.map((s) => <li key={s}>{s}</li>)}</ul>
                      ) : "None listed"}
                    </td>
                    <td className="min-w-[140px] max-w-[200px] px-2 py-3 text-xs text-muted">{r.gaps.join(", ") || "None"}</td>
                    <td className="px-2 py-3"><Chip tone={r.status === "Complete" ? "ok" : r.status === "Error" ? "stop" : "warn"}>{r.status}</Chip></td>
                    <td className="px-2 py-3"><IntegrityChip action={r.integrity_action} /></td>
                    <td className="px-2 py-3"><BandChip band={r.authenticity_band} /></td>
                    <td className="px-2 py-3"><ProseChip entries={r.provenance} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
