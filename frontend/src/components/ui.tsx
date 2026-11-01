import type { ReactNode } from 'react';
import { AlertCircle, ArrowUpRight, FileSearch, LoaderCircle, RefreshCw } from 'lucide-react';
import { errorMessage, safeHttpUrl } from '../api';
import type { RunStatus, TaskStatus } from '../types';

const statuses: Record<RunStatus | TaskStatus, [string, string]> = {
  RUNNING: ['研究中', 'running'], COMPLETED: ['已完成', 'completed'], FAILED: ['失败', 'failed'], INTERRUPTED: ['已中断', 'interrupted'],
  pending: ['待执行', 'pending'], running: ['执行中', 'running'], completed: ['已完成', 'completed'], failed: ['失败', 'failed'],
};
export function StatusBadge({ status }: { status: RunStatus | TaskStatus }) {
  const [text, style] = statuses[status] || ['未知状态', 'pending'];
  return <span className={`status status-${style}`}><span className="status-dot" />{text}</span>;
}
export function Loading({ text = '正在加载…' }: { text?: string }) {
  return <div className="loading-state" role="status"><LoaderCircle className="spin" size={22} /><span>{text}</span></div>;
}
export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  return <div className="error-notice" role="alert"><AlertCircle size={19} /><div>{errorMessage(error)}</div>
    {retry && <button className="text-button" onClick={retry}><RefreshCw size={14} />重试</button>}</div>;
}
export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><FileSearch size={26} /></div><h3>{title}</h3>{children && <div className="muted">{children}</div>}</div>;
}
export function SourceLink({ url, children, className = '' }: { url: string; children?: ReactNode; className?: string }) {
  const href = safeHttpUrl(url);
  return href ? <a className={`source-link ${className}`} href={href} target="_blank" rel="noopener noreferrer">
    <span>{children || url}</span><ArrowUpRight size={15} aria-hidden="true" /></a>
    : <span className="muted">{children || '来源地址不可安全打开'}</span>;
}
export function formatDate(date: string): string {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(date));
}
export function stageLabel(stage: string): string {
  const [base, task] = stage.split(':');
  const labels: Record<string, string> = {
    RUN_CREATED: '准备研究', PLANNING: '制定研究计划', PLAN_CREATED: '研究计划已生成',
    INITIAL_RESEARCH_BATCH: '执行研究任务', TASK_RESEARCH_COMPLETED: '任务研究已完成',
    TASK_VERIFICATION: '验证任务结论', TASK_COMPLETED: '任务已完成', INITIAL_RESEARCH_COMPLETED: '初轮研究已完成',
    GAP_DETECTION: '识别研究缺口', RELEVANCE_BATCH_COMPLETED: '相关性检查已更新', GAP_DETECTION_COMPLETED: '缺口识别已完成',
    RERESEARCH: '执行补充研究', RERESEARCH_STARTED: '补充研究已启动', RERESEARCH_CALL_RESERVED: '执行补充研究',
    RERESEARCH_COMPLETED: '补充研究已完成', WRITER: '撰写研究报告', WRITER_COMPLETED: '报告已保存',
    RERESEARCH_PLAN_CREATED: '补充研究计划已保存', RERESEARCH_TASK_RESEARCH_COMPLETED: '补充任务研究已返回',
    RERESEARCH_TASK_COMPLETED: '补充任务已完成',
    CITATION: '整理来源引用', CITATION_COMPLETED: '引用整理已完成', RUN_COMPLETED: '研究已完成',
  };
  return (labels[base] || base) + (task ? ` · ${task}` : '');
}
