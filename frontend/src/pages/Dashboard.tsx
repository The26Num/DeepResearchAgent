import { useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowRight, ArrowUpRight, BookOpen, CheckCheck, Clock3, FlaskConical, Gamepad2, LoaderCircle, RefreshCw, Search, Sparkles } from 'lucide-react';
import { api } from '../api';
import { useWorkspace } from '../components/Layout';
import { EmptyState, ErrorNotice, formatDate, Loading, stageLabel, StatusBadge } from '../components/ui';
import type { RunStatus } from '../types';

const examples = [
  { icon: FlaskConical, label: '学术研究', question: '调研 2024—2026 年离线强化学习的主要研究进展，包括主要研究方向、代表性方法、当前挑战与发展趋势' },
  { icon: Gamepad2, label: '行业研究', question: '调研 2024—2026 年中国游戏产业的发展进展，包括市场变化、产品与品类趋势、技术发展以及游戏出海情况' },
];

export function Dashboard() {
  const { history } = useWorkspace();
  const navigate = useNavigate();
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<RunStatus | 'ALL'>('ALL');
  const submitting = useRef(false);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const runs = history.data?.runs || [];
  const shown = runs.filter(run => (filter === 'ALL' || run.status === filter)
    && `${run.question} ${run.run_id}`.toLowerCase().includes(search.toLowerCase()));
  async function start(event: FormEvent) {
    event.preventDefault();
    if (!question.trim() || submitting.current) return;
    submitting.current = true; setBusy(true); setError(null);
    try {
      const run = await api.create(question.trim());
      history.refresh();
      navigate(`/runs/${encodeURIComponent(run.run_id)}`);
    } catch (err) { setError(err); }
    finally { submitting.current = false; setBusy(false); }
  }
  return <>
    <section className="dashboard-intro"><div><p className="eyebrow"><span />EVIDENCE-DRIVEN RESEARCH</p>
      <h1>让好问题，<br className="mobile-break" />找到<strong>有依据的答案。</strong></h1>
      <p className="intro-copy">从研究计划到可信来源，在一个工作台里跟进研究的每一步。</p></div>
      <div className="intro-emblem" aria-hidden="true"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><BookOpen size={34} /><span className="orbit-point" /></div>
    </section>
    <section className="question-panel" id="new-research"><form onSubmit={start}>
      <label className="field-label" htmlFor="research-question"><Sparkles size={18} />你想研究什么？</label>
      <textarea ref={textarea} id="research-question" value={question} onChange={e => setQuestion(e.target.value)} maxLength={10000} rows={4}
        placeholder="提出一个值得探索的问题，例如：离线强化学习近两年的研究有哪些新进展？" required disabled={busy} />
      <div className="question-actions"><span className="question-hint">清晰的范围，让研究更聚焦。<span className="character-count">{question.length.toLocaleString()} / 10,000</span></span>
        <button className="button primary" disabled={busy || !question.trim()} type="submit">{busy ? <LoaderCircle size={17} className="spin" /> : <Sparkles size={17} />}{busy ? '正在创建…' : '开始研究'}<ArrowRight size={17} /></button></div>
      {error != null && <div className="form-error"><ErrorNotice error={error} /></div>}
    </form></section>
    <div className="examples"><span>从一个方向开始</span>{examples.map(example => <button key={example.label} disabled={busy} onClick={() => { setQuestion(example.question); textarea.current?.focus(); }}>
      <example.icon size={15} />{example.label}<ArrowUpRight size={13} /></button>)}</div>
    <div className="stats-grid">
      {[{ label: '近期研究', value: runs.length, icon: BookOpen, style: 'neutral' },
        { label: '正在进行', value: runs.filter(r => r.status === 'RUNNING').length, icon: Clock3, style: 'amber' },
        { label: '已完成', value: runs.filter(r => r.status === 'COMPLETED').length, icon: CheckCheck, style: 'green' }].map(stat => <div className="stat-card" key={stat.label}>
          <span className={`stat-icon ${stat.style}`}><stat.icon size={21} /></span><div><span>{stat.label}</span><strong>{history.data ? stat.value : '—'}</strong></div><span className="stat-caption">最近 20 项</span></div>)}
    </div>
    <section className="history-section" id="recent"><div className="section-heading"><div><h2>最近的研究</h2><p>继续探索，或回顾已经沉淀的发现。</p></div>
      <button className="button subtle compact" onClick={history.refresh} disabled={history.refreshing}><RefreshCw size={15} className={history.refreshing ? 'spin' : ''} />刷新</button></div>
      <div className="history-toolbar"><div className="filter-group" aria-label="按研究状态筛选">{([['ALL', '全部'], ['RUNNING', '研究中'], ['COMPLETED', '已完成']] as const).map(([value, label]) =>
        <button key={value} className={filter === value ? 'selected' : ''} onClick={() => setFilter(value)} aria-pressed={filter === value}>{label}</button>)}</div>
        <label className="search-field"><Search size={16} /><input aria-label="搜索近期研究" placeholder="搜索问题或 Run ID" value={search} onChange={e => setSearch(e.target.value)} /></label></div>
      {history.error != null && <ErrorNotice error={history.error} retry={history.refresh} />}
      {history.loading ? <Loading text="正在读取研究历史…" /> : shown.length ? <div className="run-list">{shown.map(run => <Link className="run-row" key={run.run_id} to={`/runs/${encodeURIComponent(run.run_id)}`}>
        <div className={`run-row-icon ${run.status === 'COMPLETED' ? 'green' : ''}`}><BookOpen size={20} /></div>
        <div className="run-row-body"><h3>{run.question}</h3><div><span className="mono">{run.run_id.slice(0, 8)}</span><span>·</span><span>{stageLabel(run.current_stage)}</span><span>·</span><time dateTime={run.updated_at}>{formatDate(run.updated_at)}</time></div></div>
        <StatusBadge status={run.status} /><ArrowRight className="row-arrow" size={17} /></Link>)}</div>
        : !history.error && <EmptyState title={runs.length ? '没有匹配的研究' : '你的第一项研究，从这里开始'}><p>{runs.length ? '尝试其他关键词或状态筛选。' : '在上方输入问题，开始一次有据可循的探索。'}</p></EmptyState>}
    </section>
  </>;
}
