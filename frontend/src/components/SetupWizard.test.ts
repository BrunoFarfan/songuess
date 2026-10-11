import { afterEach, describe, expect, it, vi } from "vitest";
import { createFilterContextRequest, defaultFilters, requestFilterContext } from "./SetupWizard";

afterEach(() => vi.unstubAllGlobals());

describe("context request reuse", () => {
  it("normalizes unordered and repeated options without changing the selections", () => {
    const filters = {
      ...defaultFilters(null),
      genres: ["rock", "pop", "rock"],
      countries: ["US", "CL"],
    };
    const reordered = { ...filters, genres: ["pop", "rock"], countries: ["CL", "US"] };
    expect(createFilterContextRequest(filters)).toEqual(createFilterContextRequest(reordered));
    expect(filters.genres).toEqual(["rock", "pop", "rock"]);
  });

  it("shares in-flight and completed metadata across subscribers", async () => {
    const metadata = { song_count: 8 };
    let resolve!: (response: Response) => void;
    const fetchMock = vi.fn<typeof fetch>(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const key = JSON.stringify(createFilterContextRequest(defaultFilters(null)));
    const first = requestFilterContext("/test/shared-context", key);
    expect(requestFilterContext("/test/shared-context", key)).toBe(first);
    resolve(new Response(JSON.stringify(metadata)));
    await expect(first).resolves.toEqual(metadata);
    await expect(requestFilterContext("/test/shared-context", key)).resolves.toEqual(metadata);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[1]).not.toHaveProperty("signal");
  });

  it("separates endpoints and selections and retries failed requests", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("unavailable", { status: 503 }))
      .mockResolvedValue(new Response(JSON.stringify({ song_count: 3 })));
    vi.stubGlobal("fetch", fetchMock);
    await expect(requestFilterContext("/test/retry-context", "{}")).rejects.toThrow();
    await expect(requestFilterContext("/test/retry-context", "{}")).resolves.toEqual({
      song_count: 3,
    });
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ song_count: 4 })));
    await expect(requestFilterContext("/test/other-context", "{}")).resolves.toEqual({
      song_count: 4,
    });
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ song_count: 5 })));
    await expect(
      requestFilterContext("/test/other-context", '{"genres":["pop"]}'),
    ).resolves.toEqual({ song_count: 5 });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });
});
