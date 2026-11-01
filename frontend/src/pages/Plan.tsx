import { ArrowRight, Check, Circle, GitBranch, LoaderCircle, X } from 'lucide-react';
import { useRun } from './RunDetail';
import { EmptyState, StatusBadge } from '../components/ui';
import type { ResearchTask } from '../types';

function TaskCard({ task }: { task: ResearchTask }) {
  const Icon = { pending: Circle, running: LoaderCircle, completed: Check, failed: X }[task.status];
  return <article className={`task-card task-${task.status}`} id={`task-${task.id}`}>
    <div className="task-card-header"><span className={`task-state-icon ${task.status}`}><Icon size={17} className={task.status === 'running' ? 'spin' : ''} /></span>
      <span className="task-id mono">{task.id}</span><span className="task-type">{{ discovery: '资料发现', analysis: '分析研究', synthesis: '综合结论' }[task.task_type]}</span><StatusBadge status={task.status} /></div>
    <h3>{task.title}</h3><p>{task.question}</p><details><summary>任务说明</summary><p>{task.description}</p></details>
    <div className="task-dependencies"><GitBranch size={14} /><span>依赖</span>{task.depends_on.length ? task.depends_on.map(id => <a key={id} href={`#task-${encodeURIComponent(id)}`} className="dependency mono">{id}<ArrowRight size={11} /></a>) : <span className="muted">无 · 独立执行</span>}</div>
  </article>;
}

export function Plan() {
  const { run } = useRun();
  if (!run.plan) return <div className="panel"><EmptyState title="等待研究计划"><p>研究计划生成后，任务与依赖关系会显示在这里。</p></EmptyState></div>;
  const done = run.plan.tasks.filter(task => task.status === 'completed').length;
  return <><div className="section-heading"><div><h2>研究计划 <span className="count-label">{run.plan.tasks.length} 项任务</span></h2><p>查看任务分工、执行状态与前置依赖。</p></div></div>
    <div className="task-progress"><span>初始研究任务进度</span><div className="progress-track"><span style={{ width: `${run.plan.tasks.length ? done / run.plan.tasks.length * 100 : 0}%` }} /></div><strong>{done} / {run.plan.tasks.length}</strong></div>
    <div className="task-grid">{run.plan.tasks.map(task => <TaskCard key={task.id} task={task} />)}</div>
    {!!run.supplemental_tasks.length && <section className="supplemental"><div className="section-heading"><div><h2>补充研究</h2><p>针对已识别缺口展开的补充任务。</p></div></div><div className="task-grid">{run.supplemental_tasks.map(task => <TaskCard key={task.id} task={task} />)}</div></section>}
    <p className="checkpoint-note">任务状态由实时事件更新，并按已保存的检查点校准；报告写作与引用整理的进度显示在上方「当前阶段」。</p>
  </>;
}
