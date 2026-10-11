/** Share in-flight requests and completed values; failed requests remain retryable. */
export class PrefetchCache<T> {
  private entries = new Map<string, Promise<T>>();
  private expires = new Map<string, number>();

  constructor(
    private readonly limit = 24,
    private readonly ttlMs = Infinity,
  ) {}

  get(key: string, load: () => Promise<T>): Promise<T> {
    const cached = this.entries.get(key);
    if (cached && (this.expires.get(key) ?? Infinity) > Date.now()) {
      this.entries.delete(key);
      this.entries.set(key, cached);
      return cached;
    }
    if (cached) this.delete(key);
    const task = load().catch((error) => {
      if (this.entries.get(key) === task) this.delete(key);
      throw error;
    });
    this.entries.set(key, task);
    this.expires.set(key, Date.now() + this.ttlMs);
    if (this.entries.size > this.limit) this.delete(this.entries.keys().next().value!);
    return task;
  }

  delete(key: string) {
    this.entries.delete(key);
    this.expires.delete(key);
  }
}
