import { Link, Route, Routes } from 'react-router-dom';
import { Layout } from './components/Layout';
import { EmptyState } from './components/ui';
import { Dashboard } from './pages/Dashboard';
import { RunDetail } from './pages/RunDetail';
import { Plan } from './pages/Plan';
import { Report } from './pages/Report';
import { EvidenceExplorer } from './pages/EvidenceExplorer';

export function App() {
  return <Routes><Route element={<Layout />}><Route index element={<Dashboard />} />
    <Route path="runs/:runId" element={<RunDetail />}><Route index element={<Plan />} /><Route path="report" element={<Report />} /><Route path="evidence" element={<EvidenceExplorer />} /></Route>
    <Route path="*" element={<EmptyState title="这个页面不存在"><Link to="/" className="button primary">返回研究工作台</Link></EmptyState>} />
  </Route></Routes>;
}
