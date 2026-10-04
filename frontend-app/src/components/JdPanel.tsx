import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { getSampleJd } from "../api";

interface Props {
  value: string;
  onChange: (v: string) => void;
}

export default function JdPanel({ value, onChange }: Props) {
  const [touched, setTouched] = useState(false);
  const sample = useMutation({ mutationFn: getSampleJd, onSuccess: (t) => onChange(t.trim()) });
  const empty = value.trim() === "";
  const showError = touched && empty;

  return (
    <section className="card" aria-labelledby="jd-h">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 id="jd-h" className="text-sm font-semibold">1. Job description</h2>
        <button type="button" className="btn-ghost" onClick={() => sample.mutate()} disabled={sample.isPending}>
          {sample.isPending ? "Loading..." : "Load sample JD"}
        </button>
      </div>
      <label htmlFor="jd" className="sr-only">Job description</label>
      <textarea
        id="jd"
        className="input min-h-[140px] font-mono"
        placeholder="Paste the job description here"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={() => setTouched(true)}
        aria-invalid={showError}
        aria-describedby="jd-msg"
      />
      <p id="jd-msg" role={showError || sample.isError ? "alert" : undefined} className="mt-1 min-h-[18px] text-xs text-stop">
        {showError && "A job description is required before screening."}
        {sample.isError && ` Could not load the sample JD: ${(sample.error as Error).message}`}
      </p>
    </section>
  );
}
