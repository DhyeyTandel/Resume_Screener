import { useRef, useState } from "react";
import { Chip } from "./ui";

export interface FileEntry {
  id: string;
  file: File;
  github: string;
  portfolio: string;
  linkedin: File | null;
  problem: string | null;
}

export interface Consents {
  github: boolean;
  linkedin: boolean;
  portfolio: boolean;
}

export type FileState = "ready" | "queued" | "screening" | "screened" | "error";

const OK_EXT = [".pdf", ".docx", ".txt"];
const MAX_BYTES = 10 * 1024 * 1024;

export function makeEntry(file: File): FileEntry {
  const lower = file.name.toLowerCase();
  let problem: string | null = null;
  if (!OK_EXT.some((e) => lower.endsWith(e))) problem = "Unsupported type. Use PDF, DOCX or TXT.";
  else if (file.size === 0) problem = "File is empty.";
  else if (file.size > MAX_BYTES) problem = "File is larger than 10 MB.";
  return { id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 7)}`, file, github: "", portfolio: "", linkedin: null, problem };
}

interface Props {
  entries: FileEntry[];
  onEntries: (e: FileEntry[]) => void;
  paste: string;
  onPaste: (v: string) => void;
  consents: Consents;
  onConsents: (c: Consents) => void;
  fileStates: Record<string, FileState>;
  disabled: boolean;
}

const stateChip = (s: FileState | undefined) => {
  switch (s) {
    case "queued": return <Chip tone="mute">Queued</Chip>;
    case "screening": return <Chip tone="info">Screening</Chip>;
    case "screened": return <Chip tone="ok">Screened</Chip>;
    case "error": return <Chip tone="stop">Could not be read</Chip>;
    default: return <Chip tone="ok">Ready</Chip>;
  }
};

export default function ResumeUpload({ entries, onEntries, paste, onPaste, consents, onConsents, fileStates, disabled }: Props) {
  const [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const add = (list: FileList | null) => {
    if (!list || list.length === 0) return;
    onEntries([...entries, ...Array.from(list).map(makeEntry)]);
  };
  const update = (id: string, patch: Partial<FileEntry>) =>
    onEntries(entries.map((e) => (e.id === id ? { ...e, ...patch } : e)));
  const remove = (id: string) => onEntries(entries.filter((e) => e.id !== id));
  const usable = entries.filter((e) => !e.problem);

  return (
    <section className="card" aria-labelledby="resume-h">
      <h2 id="resume-h" className="mb-2 text-sm font-semibold">2. Resumes</h2>

      <div
        onDragOver={(e) => { e.preventDefault(); if (!disabled) setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); if (!disabled) add(e.dataTransfer.files); }}
        className={`rounded-lg border-2 border-dashed p-5 text-center ${drag ? "border-accent bg-accent-soft" : "border-line bg-canvas"}`}
      >
        <p className="text-muted">Drag and drop resumes here (PDF, DOCX or TXT), or</p>
        <button type="button" className="btn-ghost mt-2" disabled={disabled} onClick={() => input.current?.click()}>
          Choose files
        </button>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          accept=".pdf,.docx,.txt"
          aria-label="Upload resumes"
          onChange={(e) => { add(e.target.files); e.target.value = ""; }}
        />
      </div>

      {entries.length > 0 && (
        <ul className="mt-3 divide-y divide-line rounded-lg border border-line" aria-label="Selected resume files">
          {entries.map((e) => (
            <li key={e.id} className="flex flex-wrap items-center gap-2 px-3 py-2">
              <span className="min-w-0 flex-1 truncate font-medium">{e.file.name}</span>
              {e.problem ? <Chip tone="stop">{e.problem}</Chip> : stateChip(fileStates[e.id])}
              <button type="button" className="btn-ghost !px-2 !py-1" disabled={disabled} onClick={() => remove(e.id)} aria-label={`Remove ${e.file.name}`}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <label htmlFor="paste" className="mt-3 block text-xs font-semibold text-muted">Or paste one resume</label>
      <textarea
        id="paste"
        className="input mt-1 min-h-[90px]"
        placeholder="Paste resume text here"
        value={paste}
        disabled={disabled}
        onChange={(e) => onPaste(e.target.value)}
      />

      <details className="mt-4 rounded-lg border border-line">
        <summary className="cursor-pointer select-none px-3 py-2 font-semibold">Optional evidence sources</summary>
        <div className="space-y-4 border-t border-line p-3">
          <p className="text-xs text-muted">
            Evidence sources are attached to uploaded resume files only, one set per file. They are optional.
          </p>
          {usable.length === 0 && <p className="text-xs text-muted">Add a resume file above to attach evidence sources to it.</p>}
          {usable.map((e) => (
            <fieldset key={e.id} className="grid gap-2 sm:grid-cols-3" disabled={disabled}>
              <legend className="mb-1 text-xs font-semibold">{e.file.name}</legend>
              <div>
                <label htmlFor={`gh-${e.id}`} className="text-xs text-muted">GitHub username</label>
                <input id={`gh-${e.id}`} className="input" value={e.github} onChange={(ev) => update(e.id, { github: ev.target.value.trim() })} placeholder="octocat" />
              </div>
              <div>
                <label htmlFor={`pf-${e.id}`} className="text-xs text-muted">Portfolio URL</label>
                <input id={`pf-${e.id}`} className="input" value={e.portfolio} onChange={(ev) => update(e.id, { portfolio: ev.target.value.trim() })} placeholder="https://example.dev" />
              </div>
              <div>
                <label htmlFor={`li-${e.id}`} className="text-xs text-muted">LinkedIn export (.json or text)</label>
                <input id={`li-${e.id}`} type="file" accept=".json,.txt,.pdf" className="block w-full text-xs"
                  onChange={(ev) => update(e.id, { linkedin: ev.target.files?.[0] ?? null })} />
              </div>
            </fieldset>
          ))}

          <fieldset className="space-y-1">
            <legend className="text-xs font-semibold">Consent</legend>
            {([
              ["github", "GitHub", "Public repositories and commit history are read to look for evidence that supports skills claimed on the resume."],
              ["linkedin", "LinkedIn export", "Only the export file you upload is read, never the live profile. It is compared with the resume for date and title consistency."],
              ["portfolio", "Portfolio", "The page and a few links on it are fetched, respecting robots.txt, to find project evidence."],
            ] as const).map(([key, label, note]) => (
              <label key={key} className="flex items-start gap-2 text-[13px]">
                <input type="checkbox" className="mt-0.5" disabled={disabled} checked={consents[key]}
                  onChange={(ev) => onConsents({ ...consents, [key]: ev.target.checked })} />
                <span><strong>Consent: {label}.</strong> <span className="text-muted">{note}</span></span>
              </label>
            ))}
          </fieldset>
          <p className="text-xs text-muted">
            Sources are only used to find evidence that supports resume claims. A missing source lowers the confidence of the
            assessment. It is never counted against the candidate. Untick a box to stop that source being read.
          </p>
        </div>
      </details>
    </section>
  );
}
