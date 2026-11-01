import { useCallback } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Download, FileCheck2, Layers3 } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { api, safeHttpUrl } from '../api';
import { useResource } from '../hooks/useResource';
import { EmptyState, ErrorNotice, Loading, SourceLink } from '../components/ui';
import { useRun } from './RunDetail';

/** Render the saved Markdown verbatim. HTML, images and unsafe URLs are disabled. */
export function SafeMarkdown({ markdown }: { markdown: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
    allowedElements={['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'a', 'strong', 'em', 'del', 'code', 'pre', 'blockquote', 'ol', 'ul', 'li', 'hr', 'br', 'table', 'thead', 'tbody', 'tr', 'th', 'td']}
    urlTransform={url => safeHttpUrl(url) || ''}
    components={{ a: ({ href, children }) => safeHttpUrl(href)
      ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span> }}>{markdown}</ReactMarkdown>;
}

function SavedReportView({ runId }: { runId: string }) {
  const load = useCallback((signal: AbortSignal) => api.report(runId, signal), [runId]);
  const resource = useResource(`report:${runId}`, load);
  const data = resource.data;
  function download() {
    if (!data) return;
    const url = URL.createObjectURL(new Blob([data.markdown], { type: 'text/markdown;charset=utf-8' }));
    const anchor = document.createElement('a'); anchor.href = url;
    anchor.download = `research-${runId.replace(/[^a-zA-Z0-9_-]/g, '_')}.md`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return <>
    {resource.error != null && <ErrorNotice error={resource.error} retry={resource.refresh} />}
    {resource.loading ? <Loading text="正在读取已保存的研究报告…" /> : data && <>
      <div className="report-toolbar"><span><FileCheck2 size={16} />已保存的最终报告 <span className="toolbar-divider">·</span>{data.references.length} 个引用来源</span><button className="button subtle compact" onClick={download}><Download size={15} />下载 Markdown</button></div>
      <div className="report-layout"><article className="report-paper markdown"><SafeMarkdown markdown={data.markdown} /></article>
        <aside className="references-panel"><div className="references-heading"><h2>引用来源</h2><span>{data.references.length}</span></div><p className="muted">编号与报告中的 Citation 一致。</p>
          {data.references.length ? data.references.map(ref => <div className="reference-card" key={ref.source_id} id={`reference-${ref.citation_number}`}>
            <span className="reference-number mono">[{ref.citation_number}]</span><SourceLink url={ref.url}>{ref.title}</SourceLink>
            <span className="source-date">{ref.published_at || '日期未记录'}</span><SourceLink url={ref.url} className="reference-url">{ref.url}</SourceLink></div>) : <p className="empty-reference">当前报告没有引用来源。</p>}
          <Link className="evidence-cta" to={`/runs/${encodeURIComponent(runId)}/evidence`}><Layers3 size={18} /><span>回到证据链<br /><small>查看结论背后的原始材料</small></span><ArrowRight size={16} /></Link>
        </aside></div>
    </>}
  </>;
}

export function Report() {
  const { run } = useRun();
  return run.status === 'COMPLETED' ? <SavedReportView key={run.run_id} runId={run.run_id} /> : <div className="panel"><EmptyState title="研究报告尚未就绪">
    <p>{run.status === 'RUNNING' ? '研究完成后，已保存的报告会自动显示在这里。' : '这项研究尚未完成。可以返回研究计划查看状态与恢复选项。'}</p>
    <Link className="button subtle" to={`/runs/${encodeURIComponent(run.run_id)}`}>查看研究进度<ArrowRight size={16} /></Link></EmptyState></div>;
}
