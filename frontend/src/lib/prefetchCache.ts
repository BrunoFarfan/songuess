/** Share in-flight requests and completed values; failed requests remain retryable. */
export class PrefetchCache<T> {
  private entries = new Map<string, Promise<T>>();

  constructor(private readonly limit = 24) {}

  get(key: string, load: () => Promise<T>): Promise<T> {
    const cached = this.entries.get(key);
    if (cached) {
      this.entries.delete(key);
      this.entries.set(key, cached);
      return cached;
    }
    const task = load().catch((error) => {
      if (this.entries.get(key) === task) this.entries.delete(key);
      throw error;
    });
    this.entries.set(key, task);
    if (this.entries.size > this.limit) this.entries.delete(this.entries.keys().next().value!);
    return task;
  }

  delete(key: string) {
    this.entries.delete(key);
  }
}
