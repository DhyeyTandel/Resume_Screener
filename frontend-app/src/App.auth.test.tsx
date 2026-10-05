import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import App from "./App";
import { clearApiKey, getApiKey } from "./api";
import { jsonResponse, makeReport } from "./test/fixtures";
import { mockFetch, renderWithClient, type Handler } from "./test/utils";

const KEY = "good-key-123";
const authOf = (init?: RequestInit) => new Headers(init?.headers).get("Authorization");

/** A backend that requires KEY on everything except /v1/health. */
const locked = (required = true): Handler[] => [
  (u) => (u === "/v1/health" ? jsonResponse({ status: "ok", llm_provider: "mock", mock_mode: true, auth_required: required }) : undefined),
  (u, init) => {
    if (!required || u === "/v1/health") return undefined;
    return authOf(init) === `Bearer ${KEY}` ? undefined : jsonResponse({ code: "UNAUTHORIZED", message: "A valid API key is required." }, 401);
  },
  (u) => (u === "/v1/samples" ? jsonResponse([makeReport()]) : undefined),
  (u) => (u === "/v1/samples/jd" ? new Response("Backend Engineer\n", { status: 200 }) : undefined),
];

beforeEach(() => {
  clearApiKey();
  sessionStorage.clear();
  localStorage.clear();
});

describe("API key prompt", () => {
  it("does not appear when the server does not require a key", async () => {
    mockFetch(...locked(false));
    renderWithClient(<App />);
    expect(await screen.findByRole("button", { name: "Screen Candidates" })).toBeInTheDocument();
    expect(screen.queryByLabelText("API key")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });

  it("appears when required, rejects a wrong key, accepts the right one and sends Bearer", async () => {
    const fetchMock = mockFetch(...locked());
    const user = userEvent.setup();
    renderWithClient(<App />);
    const input = await screen.findByLabelText("API key");
    expect(input).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Show key" }));
    expect(input).toHaveAttribute("type", "text");
    await user.click(screen.getByRole("button", { name: "Hide key" }));
    expect(input).toHaveAttribute("type", "password");

    await user.type(input, "wrong");
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("That API key was not accepted.")).toBeInTheDocument();
    expect(getApiKey()).toBeNull();

    await user.clear(input);
    await user.type(input, KEY);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("button", { name: "Screen Candidates" })).toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.some(([u, i]) => u === "/v1/samples" && authOf(i as RequestInit) === `Bearer ${KEY}`)).toBe(true));
    expect(sessionStorage.length).toBe(1);
    expect(localStorage.length).toBe(0);
    expect(fetchMock.mock.calls.every(([u]) => !String(u).includes(KEY))).toBe(true);
  });

  it("re-prompts and clears the stored key when an API call returns 401", async () => {
    // The server accepts the key at sign-in time, then starts refusing it.
    let revoked = false;
    mockFetch(
      (u) => (u === "/v1/health" ? jsonResponse({ status: "ok", llm_provider: "mock", mock_mode: true, auth_required: true }) : undefined),
      (u) => (revoked && u !== "/v1/health" ? jsonResponse({ code: "UNAUTHORIZED", message: "no" }, 401) : undefined),
      (u) => (u === "/v1/samples" ? jsonResponse([makeReport()]) : undefined),
    );
    sessionStorage.setItem("screening.api_key", KEY);
    const user = userEvent.setup();
    renderWithClient(<App />);
    expect(await screen.findByRole("button", { name: "Screen Candidates" })).toBeInTheDocument();
    revoked = true;
    await user.click(screen.getByRole("button", { name: "Load sample JD" }));
    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(screen.getByText(/was not accepted/)).toBeInTheDocument();
    expect(sessionStorage.getItem("screening.api_key")).toBeNull();
  });

  it("sign out clears the key and shows the prompt; the key never reaches localStorage", async () => {
    mockFetch(...locked());
    const user = userEvent.setup();
    renderWithClient(<App />);
    await user.type(await screen.findByLabelText("API key"), KEY);
    await user.click(screen.getByRole("button", { name: "Continue" }));
    await user.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(getApiKey()).toBeNull();
    expect(sessionStorage.length).toBe(0);
    expect(JSON.stringify({ ...localStorage })).not.toContain(KEY);
  });
});
