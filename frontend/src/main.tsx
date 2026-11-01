import { Component, StrictMode, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import './styles.css';

class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <main className="fatal-error"><h1>页面暂时无法显示</h1><p>请刷新页面后重试，已保存的研究不会丢失。</p><button onClick={() => window.location.reload()}>刷新页面</button></main> : this.props.children;
  }
}

createRoot(document.getElementById('root')!).render(<StrictMode><PageBoundary><BrowserRouter><App /></BrowserRouter></PageBoundary></StrictMode>);
