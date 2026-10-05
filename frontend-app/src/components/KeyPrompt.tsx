import { useState, type FormEvent } from "react";
import { setApiKey, verifyApiKey } from "../api";

/** Shown instead of the app when the server requires an API key. The key is kept in
 * sessionStorage only (see api.ts) and travels in a header, never in a URL. */
export default function KeyPrompt({ rejected }: { rejected: boolean }) {
  const [value, setValue] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const key = value.trim();
    if (!key) {
      setProblem("Enter your API key.");
      return;
    }
    setBusy(true);
    setProblem(null);
    const result = await verifyApiKey(key);
    setBusy(false);
    if (result === "ok") setApiKey(key);
    else setProblem(result === "rejected" ? "That API key was not accepted." : "Could not reach the server. Check that the backend is running.");
  };

  const message = problem ?? (rejected ? "Your API key was not accepted. Enter it again." : null);

  return (
    <section className="card mx-auto max-w-md" aria-labelledby="key-h">
      <h2 id="key-h" className="text-sm font-semibold">Enter your API key</h2>
      <p className="mt-1 text-xs text-muted">This server requires an API key. It is kept for this browser tab only.</p>
      <form onSubmit={submit} className="mt-3 space-y-3" noValidate>
        <div>
          <label htmlFor="api-key" className="text-xs text-muted">API key</label>
          <div className="flex gap-2">
            <input
              id="api-key" className="input" type={show ? "text" : "password"} value={value}
              autoComplete="off" spellCheck={false} aria-invalid={message ? true : undefined}
              aria-describedby={message ? "api-key-err" : undefined}
              onChange={(ev) => setValue(ev.target.value)}
            />
            <button type="button" className="btn-ghost shrink-0" aria-controls="api-key" aria-pressed={show} onClick={() => setShow((s) => !s)}>
              {show ? "Hide key" : "Show key"}
            </button>
          </div>
        </div>
        {message && <p id="api-key-err" role="alert" className="text-xs text-stop">{message}</p>}
        <button type="submit" className="btn" disabled={busy}>{busy ? "Checking..." : "Continue"}</button>
      </form>
    </section>
  );
}
