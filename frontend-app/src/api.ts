import type { ApiError, AuditEntry, Health, Report, Row, Screening } from "./types";

const KEY_NAME = "screening.api_key";

/** The API key lives in sessionStorage only (gone when the tab closes), is sent in a header
 * only, and is never placed in a URL or localStorage. */
function readStored(): string | null {
  try {
    return sessionStorage.getItem(KEY_NAME);
  } catch {
    return null;
  }
}

let memoryKey: string | null = null; // fallback when sessionStorage is unavailable
let rejected = false;
let snapshot = { key: readStored(), rejected };
const listeners = new Set<() => void>();

function publish() {
  snapshot = { key: getApiKey(), rejected };
  listeners.forEach((l) => l());
}

export function getApiKey(): string | null {
  return readStored() ?? memoryKey;
}

export function setApiKey(key: string) {
  rejected = false;
  memoryKey = key;
  try {
    sessionStorage.setItem(KEY_NAME, key);
  } catch {
    /* memory fallback only */
  }
  publish();
}

export function clearApiKey(wasRejected = false) {
  rejected = wasRejected;
  memoryKey = null;
  try {
    sessionStorage.removeItem(KEY_NAME);
  } catch {
    /* ignore */
  }
  publish();
}

export const subscribeApiKey = (l: () => void) => {
  listeners.add(l);
  return () => void listeners.delete(l);
};
/** Stable object while nothing changed (required by useSyncExternalStore). Reads storage each
 * time so a key written by another tab script or a test is seen. */
export function apiKeySnapshot() {
  const key = getApiKey();
  if (key !== snapshot.key || rejected !== snapshot.rejected) snapshot = { key, rejected };
  return snapshot;
}

function withAuth(init: RequestInit = {}): RequestInit {
  const key = getApiKey();
  if (!key) return init;
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${key}`);
  return { ...init, headers };
}

/** Checks a candidate key against a protected endpoint without storing it. */
export async function verifyApiKey(key: string): Promise<"ok" | "rejected" | "unreachable"> {
  try {
    const res = await fetch("/v1/samples/jd", { headers: { Authorization: `Bearer ${key}` } });
    return res.status === 401 ? "rejected" : "ok";
  } catch {
    return "unreachable";
  }
}

export class ApiFailure extends Error {
  constructor(message: string, public remediation?: string) {
    super(message);
  }
}

async function readError(res: Response): Promise<ApiFailure> {
  if (res.status === 401) {
    // A key the server refuses is dropped at once and the UI re-prompts.
    clearApiKey(true);
    return new ApiFailure("Your API key was not accepted.", "Enter a valid key to continue.");
  }
  try {
    const body = await res.json();
    const e: ApiError = body?.detail && typeof body.detail === "object" ? body.detail : body;
    if (e?.message) return new ApiFailure(e.message, e.remediation);
    if (typeof body?.detail === "string") return new ApiFailure(body.detail);
    if (Array.isArray(body?.detail) && body.detail[0]?.msg) return new ApiFailure(body.detail[0].msg);
  } catch {
    /* not JSON */
  }
  return new ApiFailure(`Request failed (${res.status}).`);
}

async function get<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, withAuth());
  } catch {
    throw new ApiFailure("Could not reach the server. Check that the backend is running.");
  }
  if (!res.ok) throw await readError(res);
  return res.json() as Promise<T>;
}

export const getHealth = () => get<Health>("/v1/health");
export const getSamples = () => get<Report[]>("/v1/samples");
export const getScreening = (id: string) => get<Screening>(`/v1/screenings/${id}`);
export const getCandidate = (id: string) => get<Report>(`/v1/candidates/${id}`);
export const getAudit = (id: string) => get<AuditEntry[]>(`/v1/candidates/${id}/audit`);

export async function getSampleJd(): Promise<string> {
  let res: Response;
  try {
    res = await fetch("/v1/samples/jd", withAuth());
  } catch {
    throw new ApiFailure("Could not reach the server.");
  }
  if (!res.ok) throw await readError(res);
  return res.text();
}

export async function postForm<T>(path: string, fd: FormData): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, withAuth({ method: "POST", body: fd }));
  } catch {
    throw new ApiFailure("Could not reach the server. Check that the backend is running.");
  }
  if (!res.ok) throw await readError(res);
  return res.json() as Promise<T>;
}

export function recordDecision(id: string, decision: string, note: string) {
  const fd = new FormData();
  fd.append("decision", decision);
  fd.append("note", note);
  return postForm<AuditEntry>(`/v1/candidates/${id}/decision`, fd);
}

/** Same derivation as the backend `_row`, so sample reports fit the results table. */
export function rowFromReport(r: Report): Row {
  const e = r.extensions;
  return {
    candidate_id: e.candidate_id,
    candidate_name: r.candidate_name,
    overall_match_score: r.overall_match_score,
    base_score: e.score_breakdown?.base_score ?? null,
    score_confidence: e.score_breakdown?.score_confidence ?? null,
    recommendation: r.recommendation,
    strengths: (r.strengths ?? []).slice(0, 2),
    gaps: (r.missing_or_unclear_skills ?? []).slice(0, 3),
    status: e.status ?? "Complete",
    integrity_verdict: e.integrity?.intent ?? "benign",
    integrity_action: e.integrity?.recommended_action ?? "proceed",
    authenticity_band: e.authenticity?.band ?? null,
    mock_mode: e.meta?.mock_mode ?? true,
    provenance: e.meta?.provenance ?? null,
    source: "sample",
  };
}
