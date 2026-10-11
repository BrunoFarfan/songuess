import { describe, expect, it, vi } from "vitest";
import { PrefetchCache } from "./prefetchCache";

describe("prefetch cache", () => {
  it("reuses a prefetch both while pending and after it completes", async () => {
    const cache = new PrefetchCache<number>();
    let resolve!: (value: number) => void;
    const load = vi.fn(
      () =>
        new Promise<number>((done) => {
          resolve = done;
        }),
    );
    const prefetched = cache.get("page", load);
    expect(cache.get("page", load)).toBe(prefetched);
    resolve(40);
    await expect(prefetched).resolves.toBe(40);
    await expect(cache.get("page", load)).resolves.toBe(40);
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("retries failed prefetches", async () => {
    const cache = new PrefetchCache<number>();
    await expect(cache.get("cover", () => Promise.reject(new Error("offline")))).rejects.toThrow(
      "offline",
    );
    await expect(cache.get("cover", () => Promise.resolve(1))).resolves.toBe(1);
  });

  it("bounds retained entries while keeping recently consumed pages", async () => {
    const cache = new PrefetchCache<number>(2);
    await cache.get("first", () => Promise.resolve(1));
    await cache.get("tail", () => Promise.resolve(2));
    await cache.get("first", () => Promise.resolve(99));
    await cache.get("next", () => Promise.resolve(3));
    await expect(cache.get("first", () => Promise.resolve(99))).resolves.toBe(1);
    await expect(cache.get("tail", () => Promise.resolve(4))).resolves.toBe(4);
  });

  it("does not let an old failed request evict its replacement", async () => {
    const cache = new PrefetchCache<number>();
    let reject!: (reason: Error) => void;
    const old = cache.get(
      "song",
      () =>
        new Promise<number>((_, fail) => {
          reject = fail;
        }),
    );
    cache.delete("song");
    const replacement = cache.get("song", () => Promise.resolve(2));
    reject(new Error("old request failed"));
    await expect(old).rejects.toThrow();
    expect(cache.get("song", () => Promise.resolve(3))).toBe(replacement);
  });
});
