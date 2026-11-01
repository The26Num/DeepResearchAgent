import { useCallback, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ArrowDown, ArrowRight, FileText, Fingerprint, Search, ShieldCheck, Target } from 'lucide-react';
import { api } from '../api';
import { useResource } from '../hooks/useResource';
import { EmptyState, ErrorNotice, Loading, SourceLink } from '../components/ui';
import { useRun } from './RunDetail';

const supportLabels = { SUPPORTED: '已支持', PARTIAL: '部分支持', UNSUPPORTED: '未获充分支持' };
const relevanceLabels = { ALIGNED: '目标对齐', PARTIAL: '部分对齐', MISALIGNED: '偏离目标' };

export function EvidenceExplorer() {
  const { run } = useRun();
  const load = useCallback((signal: AbortSignal) => api.evidence(run.run_id, signal), [run.run_id]);
  const resource = useResource(`evidence:${run.run_id}`, load, run.status === 'RUNNING' ? 3500 : false);
  const graph = resource.data;
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState('');
  const [task, setTask] = useState('ALL');
  const filtered = (graph?.claims || []).filter(c => (task === 'ALL' || c.task_id === task)
    && `${c.text} ${c.claim_id}`.toLowerCase().includes(search.toLowerCase()));
  const selected = filtered.find(c => c.claim_id === params.get('claim')) || filtered[0];
  const verification = graph?.verification_results.find(v => v.claim_id === selected?.claim_id);
  const relevance = graph?.relevance_results.find(v => v.claim_id === selected?.claim_id);
  const evidence = new Map(graph?.evidence.map(e => [e.evidence_id, e]));
  const sources = new Map(graph?.sources.map(s => [s.source_id, s]));
  return <>
    <div className="section-heading"><div><h2>证据探索 <span className="read-only">只读</span></h2><p>沿着 Claim → Evidence → Source，查看每条结论的来源。</p></div>
      {graph && <div className="graph-counts"><span>{graph.claims.length} 结论</span><span>{graph.evidence.length} 证据</span><span>{graph.sources.length} 来源</span></div>}</div>
    {resource.error != null && <ErrorNotice error={resource.error} retry={resource.refresh} />}
    {resource.loading ? <Loading text="正在读取证据链…" /> : graph && (graph.claims.length ? <div className="evidence-layout">
      <aside className="claims-panel"><div className="claims-controls"><label className="search-field"><Search size={15} /><input aria-label="搜索结论" placeholder="搜索结论或 Claim ID" value={search} onChange={e => setSearch(e.target.value)} /></label>
        <select aria-label="筛选结论所属任务" value={task} onChange={e => setTask(e.target.value)}><option value="ALL">全部研究任务</option>{[...new Set(graph.claims.map(c => c.task_id))].map(id => <option key={id} value={id}>{id}</option>)}</select></div>
        <div className="claims-list" aria-label="结论列表">{filtered.map(claim => <button key={claim.claim_id} className={`claim-option ${claim.claim_id === selected?.claim_id ? 'selected' : ''}`}
          onClick={() => setParams({ claim: claim.claim_id }, { replace: true })} aria-pressed={claim.claim_id === selected?.claim_id}>
          <span className="claim-option-meta"><span className="mono">{claim.task_id}</span><span>{claim.evidence_ids.length} 条证据<ArrowRight size={12} /></span></span><span className="claim-option-text">{claim.text}</span></button>)}
          {!filtered.length && <EmptyState title="没有匹配的结论"><p>尝试调整关键词或任务筛选。</p></EmptyState>}</div></aside>
      <div className="evidence-detail">{selected && <>
        <section className="claim-detail panel"><p className="eyebrow"><Fingerprint size={15} />CLAIM · {selected.task_id}</p><h3>{selected.text}</h3><code className="graph-id">{selected.claim_id}</code>
          <div className="decision-grid"><div><span><ShieldCheck size={15} />证据支持</span><strong className={`decision ${verification?.status.toLowerCase() || 'pending'}`}>{verification ? supportLabels[verification.status] : '尚未验证'}</strong><p>{verification?.reason || '当前检查点没有这条结论的验证记录。'}</p></div>
            <div><span><Target size={15} />研究相关性</span><strong className={`decision ${relevance?.status.toLowerCase() || 'pending'}`}>{relevance ? relevanceLabels[relevance.status] : '尚未判断'}</strong><p>{relevance?.reason || '当前检查点没有这条结论的相关性记录。'}</p></div></div></section>
        <div className="chain-label"><ArrowDown size={16} /><span>关联的支持证据</span></div>
        {selected.evidence_ids.map(id => {
          const item = evidence.get(id);
          const source = item && sources.get(item.source_id);
          return <article className="evidence-card panel" key={id}><div className="evidence-card-header"><span><FileText size={16} />EVIDENCE</span><code>{id}</code></div>
            {item ? <><blockquote>{item.content}</blockquote>{item.location && <p className="evidence-location">定位：{item.location}</p>}
              <div className="chain-label source-chain"><ArrowDown size={14} /><span>原始来源</span></div>
              <div className="source-card">{source ? <><span className="source-type">{{ paper: '论文', webpage: '网页', repository: '代码仓库', unknown: '来源' }[source.source_type]}</span><SourceLink url={source.url}>{source.title}</SourceLink><SourceLink url={source.url} className="source-url">{source.url}</SourceLink><div className="source-footer"><span>{source.published_at || '日期未记录'}</span><code>{source.source_id}</code></div></> : <p className="muted">当前检查点中未找到对应来源：{item.source_id}</p>}</div></>
              : <p className="muted">当前检查点中未找到该证据片段。</p>}</article>;
        })}
      </>}</div></div> : <div className="panel"><EmptyState title="暂时没有可展示的结论"><p>{run.status === 'RUNNING' ? '研究过程中保存的结论与证据会逐步显示在这里。' : '当前保存的研究结果中没有正式 Claim。'}</p></EmptyState></div>)}
    <p className="checkpoint-note">此处展示已保存的材料和已有判断，读取不会重新执行 Verification。</p>
  </>;
}
