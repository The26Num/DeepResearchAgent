import { useEffect } from 'react';
import { Link, NavLink, Outlet, useLocation, useOutletContext } from 'react-router-dom';
import { ArrowUpRight, BookOpen, LayoutDashboard, Network, Plus, Radio, RefreshCw } from 'lucide-react';
import { api } from '../api';
import { useResource } from '../hooks/useResource';
import type { ResearchRun } from '../types';

export interface WorkspaceContext {
  history: ReturnType<typeof useResource<{ runs: ResearchRun[] }>>;
}
export const useWorkspace = () => useOutletContext<WorkspaceContext>();

export function Layout() {
  const location = useLocation();
  useEffect(() => {
    let target = location.hash.slice(1);
    try { target = decodeURIComponent(target); } catch { /* Treat malformed URL fragments as literal text. */ }
    if (target) document.getElementById(target)?.scrollIntoView({ behavior: 'smooth' });
    else window.scrollTo(0, 0);
  }, [location.pathname, location.hash]);
  const history = useResource('history', api.runs, 10000);
  const health = useResource('health', api.health, 30000);
  const connected = !!health.data && !health.error;
  return <div className="app-shell">
    <aside className="sidebar">
      <Link to="/" className="brand"><span className="brand-icon"><Network size={23} /></span><span>DeepResearch<small>RESEARCH WORKSPACE</small></span></Link>
      <Link to="/#new-research" className="button primary new-research"><Plus size={17} />新建研究</Link>
      <p className="nav-label">工作空间</p>
      <NavLink to="/" end className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><LayoutDashboard size={18} />研究工作台</NavLink>
      <Link to="/#recent" className="nav-item"><BookOpen size={18} />研究历史</Link>
      <div className="sidebar-recent"><p className="nav-label">最近的研究</p>
        {(history.data?.runs || []).slice(0, 4).map(run => <Link key={run.run_id} to={`/runs/${encodeURIComponent(run.run_id)}`} className="recent-link">
          <span className={`small-dot ${run.status.toLowerCase()}`} /><span>{run.question}</span></Link>)}
        {history.data?.runs.length === 0 && <p className="sidebar-empty">你的研究将在这里沉淀。</p>}
      </div>
      <div className="sidebar-bottom"><div className="sidebar-note"><Network size={20} /><p>每一条结论<br /><strong>都可以回到它的来源。</strong></p></div>
        <div className={`connection ${health.error ? 'offline' : ''}`}><Radio size={15} />{health.loading ? '正在连接服务' : connected ? '研究服务已连接' : '研究服务未连接'}
          {health.error != null && <button className="icon-button" aria-label="重新连接服务" onClick={health.refresh}><RefreshCw size={14} /></button>}</div>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar"><span>我的工作空间 <span className="slash">/</span> <strong>Research Agent</strong></span>
        <span className="topbar-note"><span className={`small-dot ${connected ? 'completed' : 'pending'}`} />让研究有据可循<ArrowUpRight size={15} /></span></header>
      <main className="main-content"><Outlet context={{ history } satisfies WorkspaceContext} /></main>
      <footer className="page-footer">DeepResearch <span>从问题到证据，保留研究的每一步。</span></footer>
    </div>
  </div>;
}
