import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

// ---------------------------------------------------------------- hash router
// Hash routes (#/videos/<id>) keep the build static: nginx or any file server can host it.

function subscribe(cb: () => void): () => void {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function useRoute(): string[] {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  return hash.replace(/^#\/?/, "").split("/").filter(Boolean);
}

export function go(path: string): void {
  window.location.hash = path.startsWith("/") ? path : `/${path}`;
}

// ---------------------------------------------------------------- data loading

export interface Loaded<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (d: T) => void;
}

/** Load once, optionally re-poll every pollMs while `poll(data)` says work is in flight. */
export function useLoad<T>(
  load: () => Promise<T>,
  deps: unknown[],
  opts: { pollMs?: number; poll?: (d: T) => boolean } = {},
): Loaded<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    try {
      const d = await loadRef.current();
      setData(d);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    setData(undefined);
    void reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const { pollMs, poll } = opts;
  const shouldPoll = pollMs !== undefined && data !== undefined && (poll ? poll(data) : true);
  useEffect(() => {
    if (!shouldPoll) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, pollMs);
    return () => window.clearInterval(id);
  }, [shouldPoll, pollMs, reload]);

  return { data, error, loading, reload, setData };
}

/** Wrap an async action with a busy flag and an error message for the UI. */
export function useAction(): {
  busy: boolean;
  error: string | null;
  run: <T>(fn: () => Promise<T>) => Promise<T | undefined>;
  clear: () => void;
} {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <T,>(fn: () => Promise<T>) => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, clear: () => setError(null) };
}
