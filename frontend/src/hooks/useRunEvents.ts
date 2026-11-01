import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { eventTypes, type Phase, type PhaseStatus, type ResearchEvent, type ResearchRun, type TaskStatus } from '../types';

export type Connection = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'unavailable' | 'closed';
interface LiveState {
  key: string; connection: Connection; events: ResearchEvent[];
  tasks: Record<string, TaskStatus>; phases: Partial<Record<Phase, PhaseStatus>>;
  stage: { value: string; timestamp: string } | null;
}
const empty = (key: string): LiveState => ({ key, connection: 'idle', events: [], tasks: {}, phases: {}, stage: null });
const terminal = new Set(['run_completed', 'run_failed', 'run_interrupted']);
const taskStates = new Set(['pending', 'running', 'completed', 'failed']);
const phases = new Set(['planning', 'research', 'verification', 'gap_detection', 'reresearch', 'writer', 'citation']);
const phaseStates = new Set(['pending', 'running', 'completed', 'failed', 'interrupted', 'skipped']);
const runStates = new Set(['RUNNING', 'FAILED', 'INTERRUPTED', 'COMPLETED']);

export function parseResearchEvent(text: string, runId: string): ResearchEvent | null {
  try {
    const value = JSON.parse(text) as ResearchEvent;
    if (value.run_id !== runId || typeof value.event_id !== 'string' || !value.event_id
        || !eventTypes.includes(value.event_type) || typeof value.stage !== 'string'
        || typeof value.message !== 'string' || value.message.length > 400
        || typeof value.timestamp !== 'string' || !Number.isFinite(Date.parse(value.timestamp)) || !value.progress_data
        || typeof value.progress_data !== 'object' || Array.isArray(value.progress_data)) return null;
    const data = value.progress_data;
    if ((data.task_status != null && !taskStates.has(data.task_status))
        || (data.run_status != null && !runStates.has(data.run_status))
        || (data.phase != null && !phases.has(data.phase))
        || (data.phase_status != null && !phaseStates.has(data.phase_status))
        || (data.tasks != null && (!Array.isArray(data.tasks) || data.tasks.length > 32
          || data.tasks.some(task => typeof task?.task_id !== 'string' || !taskStates.has(task.status))))
        || (data.phases != null && (!Array.isArray(data.phases) || data.phases.length > 7
          || data.phases.some(item => !phases.has(item?.phase) || !phaseStates.has(item.status))))) return null;
    return value;
  } catch { return null; }
}

/** One EventSource per detail page. Reconnect GETs never restart research. */
export function useRunEvents(runId: string, run: ResearchRun | null, sync: () => void, connected: { current: boolean }) {
  const enabled = run?.status === 'RUNNING';
  const attempt = run?.resume_count || 0;
  const key = `${runId}:${attempt}`;
  const syncRef = useRef(sync);
  useEffect(() => { syncRef.current = sync; }, [sync]);
  const [state, setState] = useState<LiveState>(() => empty(key));
  useEffect(() => {
    connected.current = false;
    if (!enabled) {
      setState(prev => prev.key === key ? { ...prev, connection: 'closed' } : empty(key));
      return;
    }
    if (typeof EventSource === 'undefined') {
      setState({ ...empty(key), connection: 'unavailable' });
      return;
    }
    let alive = true;
    let ended = false;
    let source: EventSource | null = null;
    let retryTimer: number | undefined;
    let syncTimer: number | undefined;
    let failures = 0;
    const seen = new Set<string>();
    setState({ ...empty(key), connection: 'connecting' });
    const requestSync = (immediate = false) => {
      if (syncTimer !== undefined) window.clearTimeout(syncTimer);
      syncTimer = window.setTimeout(() => { if (alive) syncRef.current(); }, immediate ? 0 : 120);
    };
    const reconnect = () => {
      if (!alive || ended) return;
      source?.close(); source = null;
      connected.current = false;
      setState(prev => ({ ...prev, connection: 'reconnecting' }));
      requestSync(true);
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      const delay = Math.min(1000 * 2 ** failures++, 15000);
      retryTimer = window.setTimeout(connect, delay);
    };
    const receive = (raw: MessageEvent) => {
      if (!alive || ended || typeof raw.data !== 'string' || raw.data.length > 24000) return;
      const event = parseResearchEvent(raw.data, runId);
      if (!event || seen.has(event.event_id)) return;
      seen.add(event.event_id);
      if (seen.size > 200) seen.delete(seen.values().next().value!);
      setState(prev => {
        const tasks: Record<string, TaskStatus> = Object.assign(Object.create(null), event.event_type === 'run_snapshot' ? {} : prev.tasks);
        const phases = event.event_type === 'run_snapshot' ? {} : { ...prev.phases };
        for (const task of event.progress_data.tasks || []) if (taskStates.has(task.status)) tasks[task.task_id] = task.status;
        if (event.task_id && event.progress_data.task_status && taskStates.has(event.progress_data.task_status)) tasks[event.task_id] = event.progress_data.task_status;
        for (const item of event.progress_data.phases || []) phases[item.phase] = item.status;
        if (event.progress_data.phase && event.progress_data.phase_status) phases[event.progress_data.phase] = event.progress_data.phase_status;
        const stage = ['run_snapshot', 'stream_reset', 'stream_error'].includes(event.event_type) ? prev.stage : { value: event.stage, timestamp: event.timestamp };
        return { ...prev, tasks, phases, stage, events: [...prev.events, event].slice(-100) };
      });
      if (event.event_type === 'stream_error') { reconnect(); return; }
      const finalSnapshot = event.event_type === 'run_snapshot' && event.progress_data.run_status && event.progress_data.run_status !== 'RUNNING';
      if (terminal.has(event.event_type) || finalSnapshot) {
        ended = true; connected.current = false; source?.close(); source = null;
        if (retryTimer !== undefined) window.clearTimeout(retryTimer);
        setState(prev => ({ ...prev, connection: 'closed' }));
        requestSync(true); // Only REST/SQLite can put the run into a terminal state.
      } else if (['run_snapshot', 'stream_reset', 'plan_created', 'task_completed', 'task_failed',
        'reresearch_plan_created', 'report_completed'].includes(event.event_type)) requestSync();
    };
    function connect() {
      if (!alive || ended) return;
      try {
        const next = new EventSource(api.eventsUrl(runId));
        source = next;
        next.onopen = () => {
          if (!alive || ended || source !== next) return;
          connected.current = true; failures = 0;
          setState(prev => ({ ...prev, connection: 'connected' }));
          requestSync(); // Every connection/reconnection heals from SQLite.
        };
        for (const type of eventTypes) next.addEventListener(type, receive as EventListener);
        next.onerror = () => { if (source === next) reconnect(); };
      } catch { reconnect(); }
    }
    connect();
    return () => {
      alive = false; connected.current = false; source?.close();
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      if (syncTimer !== undefined) window.clearTimeout(syncTimer);
    };
  }, [runId, key, enabled, connected]);
  return state.key === key ? state : empty(key);
}

export function withLiveTasks(run: ResearchRun, tasks: Record<string, TaskStatus>): ResearchRun {
  if (run.status === 'COMPLETED') return run;
  const update = (task: NonNullable<ResearchRun['plan']>['tasks'][number]) => {
    const live = Object.hasOwn(tasks, task.id) ? tasks[task.id] : undefined;
    // A committed completion always wins over a delayed live event.
    return live && task.status !== 'completed' ? { ...task, status: live } : task;
  };
  return { ...run, plan: run.plan && { ...run.plan, tasks: run.plan.tasks.map(update) }, supplemental_tasks: run.supplemental_tasks.map(update) };
}
