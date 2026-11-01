import { Activity, Check, Circle, LoaderCircle, Radio, WifiOff, X } from 'lucide-react';
import type { Connection } from '../hooks/useRunEvents';
import type { Phase, PhaseStatus, ResearchEvent, ResearchRun } from '../types';

const labels: Record<Phase, string> = { planning: '研究规划', research: '资料研究', verification: '结论验证', gap_detection: '缺口检查', reresearch: '补充研究', writer: '报告写作', citation: '引用整理' };
const statusLabels: Record<PhaseStatus, string> = { pending: '待执行', running: '进行中', completed: '已完成', failed: '失败', interrupted: '已中断', skipped: '未启用' };

export function StreamConnection({ state, active }: { state: Connection; active: boolean }) {
  if (!active) return <span className="stream-connection"><Check size={13} />已同步保存状态</span>;
  const text: Record<Connection, string> = { idle: '准备实时连接', connecting: '正在连接实时进度', connected: '实时进度已连接', reconnecting: '实时连接重试中 · 状态查询继续', unavailable: '实时连接不可用 · 状态查询继续', closed: '正在同步最终状态' };
  return <span className={`stream-connection ${state === 'connected' ? 'online' : 'recovering'}`} role="status">{state === 'connected' ? <Radio size={13} /> : <WifiOff size={13} />}{text[state]}</span>;
}

export function PhaseProgress({ run, observed }: { run: ResearchRun; observed: Partial<Record<Phase, PhaseStatus>> }) {
  if (!run.phase_statuses?.length && !Object.keys(observed).length) return null;
  return <section className="phase-progress" aria-label="研究阶段状态">{(Object.keys(labels) as Phase[]).map(phase => {
    const saved = run.phase_statuses?.find(item => item.phase === phase)?.status;
    const status = run.status !== 'RUNNING' || saved === 'completed' || saved === 'skipped' ? saved || observed[phase] || 'pending' : observed[phase] || saved || 'pending';
    const Icon = status === 'completed' ? Check : status === 'running' ? LoaderCircle : status === 'failed' ? X : Circle;
    return <div className={`phase-progress-item phase-${status}`} key={phase}><Icon size={14} className={status === 'running' ? 'spin' : ''} /><span>{labels[phase]}<small>{statusLabels[status]}</small></span></div>;
  })}</section>;
}

export function ExecutionTimeline({ events }: { events: ResearchEvent[] }) {
  const visible = events.filter(event => !['run_snapshot', 'stream_reset', 'stream_error'].includes(event.event_type));
  return <section className="execution-timeline panel"><div className="timeline-heading"><h2><Activity size={16} />执行时间线</h2><span>本页最多保留 100 条实时事件</span></div>
    {!visible.length ? <p className="timeline-empty">从本次连接开始记录执行事件；已保存的进度可在上方查看。</p> : <ol>{visible.map(event => {
      const failed = event.event_type.endsWith('failed');
      const time = new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(event.timestamp));
      return <li key={event.event_id} className={failed ? 'timeline-failed' : ''}><span className="timeline-dot" /><time dateTime={event.timestamp}>{time}</time><div>{event.task_id && <code>{event.task_id}</code>}<span>{event.message}</span></div>
        {typeof event.progress_data.elapsed_seconds === 'number' && <small>{event.progress_data.elapsed_seconds.toFixed(1)}s</small>}</li>;
    })}</ol>}
  </section>;
}
