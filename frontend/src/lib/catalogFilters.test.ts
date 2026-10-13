import { afterEach, describe, expect, it, vi } from "vitest";
import { loadCatalogFilters } from "./catalogFilters";

const metadata = {
  genres: ["Rock"],
  countries: ["CL"],
  year_min: 1960,
  year_max: 2026,
  popularity_min: 0,
  popularity_max: 100,
  song_count: 10,
};

afterEach(() => vi.useRealTimers());

describe("catalog filter loading", () => {
  it("recovers from a network failure and a temporary server failure", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const request = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(new Response(null, { status: 503 }))
      .mockResolvedValueOnce(Response.json(metadata));
    const result = loadCatalogFilters(controller.signal, request);
    await vi.advanceTimersByTimeAsync(249);
    expect(request).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(request).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(750);
    await expect(result).resolves.toEqual(metadata);
    expect(request).toHaveBeenCalledTimes(3);
    expect(request).toHaveBeenLastCalledWith("/api/filters", { signal: controller.signal });
  });

  it.each([500, 502, 503])("stops after three attempts for HTTP %s", async (status) => {
    vi.useFakeTimers();
    const request = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status }));
    const result = loadCatalogFilters(new AbortController().signal, request);
    const rejection = expect(result).rejects.toThrow("Could not load the catalog filters.");
    await vi.runAllTimersAsync();
    await rejection;
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("does not retry a rejected request or invalid JSON response", async () => {
    const rejected = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 400 }));
    await expect(loadCatalogFilters(new AbortController().signal, rejected)).rejects.toThrow();
    expect(rejected).toHaveBeenCalledTimes(1);

    const invalid = vi.fn<typeof fetch>().mockResolvedValue(new Response("invalid JSON"));
    await expect(loadCatalogFilters(new AbortController().signal, invalid)).rejects.toThrow();
    expect(invalid).toHaveBeenCalledTimes(1);
  });

  it("cancels a retry delay when the component unmounts", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    const request = vi.fn<typeof fetch>().mockRejectedValue(new TypeError("offline"));
    const result = loadCatalogFilters(controller.signal, request);
    const rejection = expect(result).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();
    await rejection;
    await vi.runAllTimersAsync();
    expect(request).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not deliver an in-flight response after cancellation", async () => {
    const controller = new AbortController();
    let resolve!: (response: Response) => void;
    const request = vi.fn<typeof fetch>(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    const result = loadCatalogFilters(controller.signal, request);
    controller.abort();
    resolve(Response.json(metadata));
    await expect(result).rejects.toMatchObject({ name: "AbortError" });
    expect(request).toHaveBeenCalledTimes(1);
  });
});
