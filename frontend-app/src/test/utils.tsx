import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import { jsonResponse } from "./fixtures";

export type Handler = (url: string, init?: RequestInit) => Response | Promise<Response> | undefined;

/** Stubs global fetch. Handlers are tried in order; an unhandled URL fails the test loudly. */
export function mockFetch(...handlers: Handler[]) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    for (const h of handlers) {
      const r = await h(url, init);
      if (r) return r;
    }
    return jsonResponse({ detail: `unmocked ${url}` }, 500);
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

export function renderWithClient(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}
