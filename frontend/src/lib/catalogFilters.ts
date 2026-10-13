export type FilterMetadata = {
  genres: string[];
  countries: string[];
  year_min: number | null;
  year_max: number | null;
  popularity_min: number;
  popularity_max: number;
  song_count: number;
};

const RETRY_DELAYS_MS = [250, 750];
const RETRYABLE_STATUSES = new Set([500, 502, 503]);

function waitForRetry(delay: number, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, delay);
    signal.addEventListener("abort", abort, { once: true });
  });
}

export async function loadCatalogFilters(
  signal: AbortSignal,
  fetchFilters: typeof fetch = fetch,
): Promise<FilterMetadata> {
  for (let attempt = 0; ; attempt += 1) {
    signal.throwIfAborted();
    let response: Response | undefined;
    let requestError: unknown;
    try {
      response = await fetchFilters("/api/filters", { signal });
    } catch (error) {
      requestError = error;
    }
    signal.throwIfAborted();

    if (response?.ok) {
      // Parsing failures are not transient request failures and should not retry.
      const metadata = (await response.json()) as FilterMetadata;
      signal.throwIfAborted();
      return metadata;
    }

    const error = requestError ?? new Error("Could not load the catalog filters.");
    const retryable = !response || RETRYABLE_STATUSES.has(response.status);
    if (!retryable || attempt >= RETRY_DELAYS_MS.length) throw error;
    await waitForRetry(RETRY_DELAYS_MS[attempt], signal);
  }
}
