import { useCallback, useEffect, useState } from 'react';
import { ApiError } from '../api';

type Interval<T> = number | false | ((data: T | null) => number | false);
interface State<T> { key: string; data: T | null; error: unknown; loading: boolean; refreshing: boolean }

/** Sequential polling; abort on unmount/navigation and ignore stale responses. */
export function useResource<T>(key: string, load: (signal: AbortSignal) => Promise<T>, interval: Interval<T> = false) {
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<State<T>>({ key, data: null, error: null, loading: true, refreshing: false });
  const refresh = useCallback(() => setRevision(n => n + 1), []);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController;
    let latest: T | null = null;
    async function fetchData() {
      controller = new AbortController();
      setState(prev => ({ key, data: prev.key === key ? prev.data : null, error: null,
        loading: prev.key !== key || !prev.data, refreshing: true }));
      let retry = true;
      try {
        const data = await load(controller.signal);
        if (!alive) return;
        latest = data;
        setState({ key, data, error: null, loading: false, refreshing: false });
      } catch (error) {
        if (!alive) return;
        retry = !(error instanceof ApiError && [404, 409, 422].includes(error.status));
        setState(prev => ({ key, data: prev.key === key ? prev.data : null, error,
          loading: false, refreshing: false }));
      }
      const delay = typeof interval === 'function' ? interval(latest) : interval;
      if (alive && retry && delay !== false) timer = setTimeout(fetchData, delay);
    }
    void fetchData();
    return () => { alive = false; controller?.abort(); if (timer) clearTimeout(timer); };
  }, [key, load, interval, revision]);
  return { ...(state.key === key ? state : { key, data: null, error: null, loading: true, refreshing: false }), refresh };
}
