import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, NavLink, Outlet, useLocation, useNavigate, useOutletContext, useParams } from 'react-router-dom';
import { ArrowLeft, Check, Clock3, Copy, FileText, GitBranch, Layers3, LoaderCircle, RefreshCw, RotateCcw } from 'lucide-react';
import { api } from '../api';
import { useResource } from '../hooks/useResource';
import { useRunEvents, withLiveTasks } from '../hooks/useRunEvents';
import { ExecutionTimeline, PhaseProgress, StreamConnection } from '../components/RunProgress';
import { EmptyState, ErrorNotice, formatDate, Loading, stageLabel, StatusBadge } from '../components/ui';
import type { ResearchRun } from '../types';

interface RunContext { run: ResearchRun; refreshRun: () => void }
export const useRun = () => useOutletContext<RunContext>();

export function RunDetail() {
  const { runId = '' } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const previous = useRef<{ id: string; status: string } | null>(null);
  const streamConnected = useRef(false);
  const pollRun = useCallback((run: ResearchRun | null) => !run || run.status === 'RUNNING' ? streamConnected.current ? 15000 : 2500 : false, []);
  const fetchRun = useCallback((signal: AbortSignal) => api.run(runId, signal), [runId]);
  const resource = useResource(runId, fetchRun, pollRun);
  const stream = useRunEvents(runId, resource.data, resource.refresh, streamConnected);
  const run = resource.data && withLiveTasks(resource.data, stream.tasks);
  const currentStage = run && run.status === 'RUNNING' && stream.stage
    && Date.parse(stream.stage.timestamp) >= Date.parse(run.updated_at) ? stream.stage.value : run?.current_stage || '';
  useEffect(() => {
    if (!run) return;
    if (previous.current?.id === runId && previous.current.status === 'RUNNING' && run.status === 'COMPLETED'
        && location.pathname.replace(/\/$/, '') === `/runs/${encodeURIComponent(runId)}`) {
      navigate(`/runs/${encodeURIComponent(runId)}/report`, { replace: true });
    }
    previous.current = { id: runId, status: run.status };
  }, [runId, run?.status, location.pathname, navigate]);
  const [resuming, setResuming] = useState(false);
  const [resumeError, setResumeError] = useState<unknown>(null);
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');
  const submitting = useRef(false);
  async function resume() {
    if (submitting.current || !run?.resumable) return;
    submitting.current = true; setResuming(true); setResumeError(null);
    try { await api.resume(runId); resource.refresh(); }
    catch (error) { setResumeError(error); resource.refresh(); }
    finally { submitting.current = false; setResuming(false); }
  }
  async function copy() {
    try { await navigator.clipboard.writeText(runId); setCopyState('copied'); }
    catch { setCopyState('failed'); }
  }
  return <>
    <Link className="back-link" to="/"><ArrowLeft size={16} />返回研究工作台</Link>
    {resource.error != null && <ErrorNotice error={resource.error} retry={resource.refresh} />}
    {resource.loading ? <Loading text="正在读取研究状态…" /> : !run ? <EmptyState title="暂时无法读取这项研究"><p>请检查服务连接或返回工作台。</p></EmptyState> : <>
      <div className="run-heading"><div><p className="eyebrow">RESEARCH RUN</p><h1>{run.question}</h1></div><StatusBadge status={run.status} /></div>
      <div className="run-id"><span>Run ID</span><code>{run.run_id}</code><button className="icon-button" onClick={copy} aria-label="复制 Run ID">{copyState === 'copied' ? <Check size={14} /> : <Copy size={14} />}</button>{copyState === 'copied' && <span>已复制</span>}{copyState === 'failed' && <span>请选中 ID 手动复制</span>}</div>
      <StreamConnection state={stream.connection} active={run.status === 'RUNNING'} />
      <div className="run-meta-grid"><div><span><Layers3 size={15} />当前阶段</span><strong>{stageLabel(currentStage)}</strong></div>
        <div><span><Clock3 size={15} />最近更新</span><strong>{formatDate(run.updated_at)}</strong></div>
        <div><span><GitBranch size={15} />初始研究任务</span><strong>{run.plan ? `${run.plan.tasks.filter(t => t.status === 'completed').length} / ${run.plan.tasks.length} 已完成` : '等待研究计划'}</strong></div>
        <div className="run-meta-actions"><button className="button subtle compact" onClick={resource.refresh} disabled={resource.refreshing}><RefreshCw size={14} className={resource.refreshing ? 'spin' : ''} />刷新状态</button></div></div>
      {run.error && <div className="run-failure"><div><strong>{run.status === 'INTERRUPTED' ? '研究已中断' : '研究执行失败'}</strong><p>{run.error.code === 'RESEARCH_TIMEOUT' ? '研究请求超时。已保存的检查点仍然保留。' : '本次研究未能完成。已保存的进度仍然保留。'}</p></div>
        {run.resumable && <button className="button primary compact" onClick={resume} disabled={resuming}>{resuming ? <LoaderCircle className="spin" size={15} /> : <RotateCcw size={15} />}{resuming ? '正在恢复…' : '从检查点恢复'}</button>}</div>}
      {resumeError != null && <ErrorNotice error={resumeError} />}
      <PhaseProgress run={run} observed={stream.phases} />
      <nav className="detail-tabs" aria-label="研究详情导航"><NavLink to={`/runs/${encodeURIComponent(runId)}`} end><GitBranch size={17} />研究计划</NavLink>
        <NavLink to="report"><FileText size={17} />研究报告</NavLink><NavLink to="evidence"><Layers3 size={17} />证据探索</NavLink></nav>
      <Outlet context={{ run, refreshRun: resource.refresh } satisfies RunContext} />
      <ExecutionTimeline events={stream.events} />
    </>}
  </>;
}
