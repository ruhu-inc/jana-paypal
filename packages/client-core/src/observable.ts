// A value for `useSyncExternalStore`: `get` returns the same object until `set` replaces it.
type Listener = () => void;

export class Observable<T> {
  private readonly listeners = new Set<Listener>();

  constructor(private value: T) {}

  get = (): T => this.value;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  set(next: T): void {
    if (next === this.value) return;
    this.value = next;
    for (const listener of [...this.listeners]) listener();
  }
}
