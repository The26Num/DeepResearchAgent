import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { running } from '../../tests/fixtures';
import type { ResearchEvent } from '../types';
import { parseResearchEvent, useRunEvents, withLiveTasks } from './useRunEvents';

class Source {
  static all: Source[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, EventListener[]>();
  constructor(public url: string) { Source.all.push(this); }
  addEventListener(type: string, listener: EventListener) { this.listeners.set(type, [...(this.listeners.get(type) || []), listener]); }
  close() { this.closed = true; }
  emit(event: ResearchEvent) { for (const listener of this.listeners.get(event.event_type) || []) listener(new MessageEvent(event.event_type, { data: JSON.stringify(event) })); }
}
let sequence = 0;
function event(type: ResearchEvent['event_type'], progress: ResearchEvent['progress_data'] = {}, overrides = {}): ResearchEvent {
  return { run_id: running.run_id, event_id: String(++sequence), event_type: type, timestamp: new Date().toISOString(),
    task_id: null, stage: 'INITIAL_RESEARCH_BATCH', message: '实时阶段事件', progress_data: progress, ...overrides };
}
beforeEach(() => { vi.useFakeTimers(); Source.all = []; vi.stubGlobal('EventSource', Source); });
afterEach(() => vi.useRealTimers());

describe('run SSE', () => {
  it('updates live tasks, retains research-return as running, and de-duplicates IDs', () => {
    const sync = vi.fn(), connected = { current: false };
    const { result, unmount } = renderHook(() => useRunEvents(running.run_id, running, sync, connected));
    const source = Source.all[0];
    expect(source.url).toBe('/api/runs/running-run/events');
    const started = event('task_started', { task_status: 'running' }, { task_id: 'D1' });
    act(() => { source.onopen?.(); source.emit(started); source.emit(started); });
    expect(connected.current).toBe(true);
    expect(result.current.tasks.D1).toBe('running');
    expect(result.current.events).toHaveLength(1);
    act(() => source.emit(event('task_research_completed', { task_status: 'running' }, { task_id: 'D1' })));
    expect(result.current.tasks.D1).toBe('running');
    act(() => source.emit(event('task_completed', { task_status: 'completed' }, { task_id: 'D1' })));
    expect(result.current.tasks.D1).toBe('completed');
    unmount();
    expect(source.closed).toBe(true);
  });
  it('reconnects with backoff and syncs REST after reconnect', () => {
    const sync = vi.fn(), connected = { current: false };
    const { result, unmount } = renderHook(() => useRunEvents(running.run_id, running, sync, connected));
    act(() => Source.all[0].onopen?.());
    act(() => vi.advanceTimersByTime(150));
    expect(sync).toHaveBeenCalledOnce();
    act(() => Source.all[0].onerror?.());
    expect(connected.current).toBe(false);
    expect(result.current.connection).toBe('reconnecting');
    expect(Source.all[0].closed).toBe(true);
    act(() => vi.advanceTimersByTime(1000));
    expect(Source.all).toHaveLength(2);
    act(() => Source.all[1].onopen?.());
    act(() => vi.advanceTimersByTime(150));
    expect(result.current.connection).toBe('connected');
    expect(sync.mock.calls.length).toBeGreaterThanOrEqual(2);
    unmount();
  });
  it('heals snapshot overlays and never trusts events from another run', () => {
    const connected = { current: false };
    const sync = vi.fn();
    const { result, unmount } = renderHook(() => useRunEvents(running.run_id, running, sync, connected));
    act(() => Source.all[0].emit(event('task_started', { task_status: 'running' }, { task_id: 'A' })));
    act(() => Source.all[0].emit(event('task_completed', { task_status: 'completed' }, { run_id: 'other', task_id: 'A' })));
    expect(result.current.tasks.A).toBe('running');
    act(() => Source.all[0].emit(event('run_snapshot', { tasks: [{ task_id: 'D1', status: 'completed' }], phases: [{ phase: 'writer', status: 'running' }] })));
    expect(result.current.tasks.A).toBeUndefined();
    expect(result.current.tasks.D1).toBe('completed');
    expect(result.current.phases.writer).toBe('running');
    unmount();
  });
  it('closes after a terminal event and only asks REST to confirm final state', () => {
    const sync = vi.fn(), connected = { current: false };
    const { result, unmount } = renderHook(() => useRunEvents(running.run_id, running, sync, connected));
    act(() => Source.all[0].emit(event('run_completed', { run_status: 'COMPLETED' })));
    act(() => vi.advanceTimersByTime(20000));
    expect(Source.all).toHaveLength(1);
    expect(Source.all[0].closed).toBe(true);
    expect(result.current.connection).toBe('closed');
    expect(sync).toHaveBeenCalledOnce();
    expect(running.status).toBe('RUNNING');
    unmount();
  });
  it('cleans up retries and ignores events after navigation/unmount', () => {
    const connected = { current: false };
    const sync = vi.fn();
    const { result, unmount } = renderHook(() => useRunEvents(running.run_id, running, sync, connected));
    const source = Source.all[0];
    act(() => source.onerror?.());
    unmount();
    act(() => { vi.advanceTimersByTime(20000); source.emit(event('task_started', { task_status: 'running' }, { task_id: 'A' })); });
    expect(Source.all).toHaveLength(1);
    expect(result.current.tasks.A).toBeUndefined();
    expect(connected.current).toBe(false);
  });
  it('bounds the timeline and keeps the SQL completion authoritative', () => {
    const connected = { current: false };
    const sync = vi.fn();
    const { result, unmount } = renderHook(() => useRunEvents(running.run_id, running, sync, connected));
    act(() => { for (let i = 0; i < 140; i++) Source.all[0].emit(event('stage_changed')); });
    expect(result.current.events).toHaveLength(100);
    const displayed = withLiveTasks(running, { D1: 'running', D2: 'completed' });
    expect(displayed.plan?.tasks[0].status).toBe('completed');
    expect(displayed.plan?.tasks[1].status).toBe('completed');
    expect(withLiveTasks({ ...running, status: 'COMPLETED' }, { D1: 'failed' }).plan?.tasks[0].status).toBe('completed');
    unmount();
  });
  it('rejects invalid envelopes and phase payloads', () => {
    expect(parseResearchEvent('not json', running.run_id)).toBeNull();
    expect(parseResearchEvent(JSON.stringify(event('task_started', {}, { run_id: 'other' })), running.run_id)).toBeNull();
    expect(parseResearchEvent(JSON.stringify(event('run_snapshot', { phases: [{ phase: '__proto__', status: 'running' } as never] })), running.run_id)).toBeNull();
  });
  it('keeps the connection when a new render supplies a different sync callback', () => {
    const connected = { current: false };
    const callbacks = [vi.fn(), vi.fn()];
    const { rerender, unmount } = renderHook(({ sync }) => useRunEvents(running.run_id, running, sync, connected),
      { initialProps: { sync: callbacks[0] } });
    rerender({ sync: callbacks[1] });
    expect(Source.all).toHaveLength(1);
    act(() => Source.all[0].onopen?.());
    act(() => vi.advanceTimersByTime(150));
    expect(callbacks[0]).not.toHaveBeenCalled();
    expect(callbacks[1]).toHaveBeenCalledOnce();
    unmount();
  });
});
