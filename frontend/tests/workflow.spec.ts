import { expect, test, type Page } from '@playwright/test';
import { completed, failed, graph, report, running } from './fixtures';
import type { ResearchEvent } from '../src/types';

async function backend(page: Page) {
  const state = { complete: false, createError: false, historyError: false, statusError: false, reportMarkdown: report.markdown,
    created: [] as unknown[], resumed: 0, runReads: 0, reportReads: 0, resumedRun: false };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    const reply = (json: unknown, status = 200) => route.fulfill({ json, status });
    if (path === '/api/health') return reply({ status: 'ok' });
    if (path === '/api/runs' && method === 'GET') return state.historyError ? reply({ error: { code: 'INTERNAL_ERROR' } }, 500) : reply({ runs: [completed, running, failed] });
    if (path === '/api/runs' && method === 'POST') {
      state.created.push(route.request().postDataJSON());
      return state.createError ? reply({ error: { code: 'RUN_CAPACITY_EXCEEDED' } }, 429) : reply({ run_id: 'created-run', status: 'RUNNING' }, 202);
    }
    const match = path.match(/^\/api\/runs\/([^/]+)(?:\/(report|evidence|resume))?$/);
    if (!match || match[1] === 'missing') return reply({ error: { code: 'RUN_NOT_FOUND' } }, 404);
    const [, id, action] = match;
    if (action === 'resume') { state.resumed++; state.resumedRun = true; return reply({ run_id: id, status: 'RUNNING' }, 202); }
    if (action === 'report') { state.reportReads++; return reply({ ...report, run_id: id, markdown: state.reportMarkdown }); }
    if (action === 'evidence') return reply({ ...graph, run_id: id });
    state.runReads++;
    if (state.statusError) return reply({ error: { code: 'INTERNAL_ERROR' } }, 500);
    const item = id === 'completed-run' || state.complete ? completed : id === 'failed-run' && !state.resumedRun ? failed : running;
    return reply({ ...item, run_id: id });
  });
  return state;
}

test('dashboard shows real API history and creates a run before navigating', async ({ page }) => {
  const state = await backend(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: '最近的研究' })).toBeVisible();
  await expect(page.getByRole('link', { name: completed.question }).last()).toBeVisible();
  await expect(page.getByRole('button', { name: '开始研究' })).toBeDisabled();
  await page.getByLabel('你想研究什么？').fill('调研持续学习的新进展');
  await page.getByRole('button', { name: '开始研究' }).click();
  await expect(page).toHaveURL('/runs/created-run');
  await expect(page.getByRole('heading', { name: '研究计划' })).toBeVisible();
  expect(state.created).toEqual([{ question: '调研持续学习的新进展' }]);
});

test('example questions only fill the input and do not start research', async ({ page }) => {
  const state = await backend(page);
  await page.goto('/');
  await page.getByRole('button', { name: '学术研究' }).click();
  await expect(page.getByLabel('你想研究什么？')).toHaveValue(/2024—2026 年离线强化学习/);
  expect(state.created).toHaveLength(0);
});

test('detail shows task state and dependency links', async ({ page }) => {
  await backend(page);
  await page.goto('/runs/running-run');
  await expect(page.getByText('执行研究任务', { exact: true })).toBeVisible();
  await expect(page.locator('.task-card')).toHaveCount(4);
  const analysis = page.locator('#task-A');
  await expect(analysis.getByRole('link', { name: 'D1' })).toHaveAttribute('href', '#task-D1');
  await expect(analysis.getByRole('link', { name: 'D2' })).toHaveAttribute('href', '#task-D2');
  await expect(page.locator('#task-D2').getByText('执行中', { exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/run-detail-desktop.png', fullPage: true });
});

test('polling notices completion and reads the saved cited report', async ({ page }) => {
  const state = await backend(page);
  await page.goto('/runs/running-run/report');
  await expect(page.getByRole('heading', { name: '研究报告尚未就绪' })).toBeVisible();
  expect(state.reportReads).toBe(0);
  state.complete = true;
  await expect(page.getByRole('heading', { name: '离线强化学习研究报告' })).toBeVisible({ timeout: 8000 });
  await expect(page.locator('.markdown')).toContainText('离线强化学习依赖既有数据进行策略学习。 [1]');
  await expect(page.getByRole('link', { name: 'https://arxiv.org/abs/2401.00001' }).last()).toHaveAttribute('href', 'https://arxiv.org/abs/2401.00001');
  const requests = state.runReads;
  await page.waitForTimeout(3000);
  expect(state.runReads).toBe(requests);
});

test('direct report route survives refresh with saved citations and references', async ({ page }) => {
  const state = await backend(page);
  await page.goto('/runs/completed-run/report');
  await expect(page.getByRole('heading', { name: 'References', exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/report-desktop.png', fullPage: true });
  await page.reload();
  await expect(page.getByRole('heading', { name: '离线强化学习研究报告' })).toBeVisible();
  await expect(page.locator('.reference-number')).toHaveText('[1]');
  await expect(page.getByRole('button', { name: '从检查点恢复' })).toHaveCount(0);
  expect(state.created).toHaveLength(0);
});

test('evidence explorer follows the stored chain and missing judgments stay unknown', async ({ page }) => {
  await backend(page);
  await page.goto('/runs/completed-run/evidence');
  await expect(page.getByRole('heading', { name: '离线强化学习依赖既有数据进行策略学习。' })).toBeVisible();
  await expect(page.getByText('Offline reinforcement learning learns from previously collected data.')).toBeVisible();
  await expect(page.locator('.decision.supported')).toHaveText('已支持');
  await expect(page.locator('.source-card a').first()).toHaveAttribute('href', graph.sources[0].url);
  await page.screenshot({ path: 'test-results/evidence-desktop.png', fullPage: true });
  await page.getByRole('button', { name: /数据覆盖范围是研究中需要考虑的因素/ }).click();
  await expect(page).toHaveURL(/claim=claim_002/);
  await expect(page.getByText('尚未验证', { exact: true })).toBeVisible();
  await expect(page.getByText('尚未判断', { exact: true })).toBeVisible();
  await expect(page.locator('.source-card a').first()).toHaveAttribute('href', graph.sources[1].url);
  await page.reload();
  await expect(page.getByRole('heading', { name: '数据覆盖范围是研究中需要考虑的因素。' })).toBeVisible();
});

test('creation failure is readable and never retries the POST automatically', async ({ page }) => {
  const state = await backend(page); state.createError = true;
  await page.goto('/');
  await page.getByLabel('你想研究什么？').fill('保留这个问题');
  await page.getByRole('button', { name: '开始研究' }).click();
  await expect(page.getByRole('alert')).toContainText('研究席位已满');
  await expect(page.getByLabel('你想研究什么？')).toHaveValue('保留这个问题');
  expect(state.created).toHaveLength(1);
  await expect(page).toHaveURL('/');
});

test('404 and history failures have explicit error states', async ({ page }) => {
  const state = await backend(page);
  await page.goto('/runs/missing');
  await expect(page.getByRole('alert')).toContainText('没有找到这项研究');
  state.historyError = true;
  await page.goto('/');
  await expect(page.getByRole('alert')).toContainText('服务暂时无法处理请求');
  await expect(page.getByRole('heading', { name: '你的第一项研究，从这里开始' })).toHaveCount(0);
});

test('resume is explicit and uses the existing endpoint once', async ({ page }) => {
  const state = await backend(page);
  await page.goto('/runs/failed-run');
  await page.getByRole('button', { name: '从检查点恢复' }).click();
  await expect(page.locator('.run-heading .status')).toHaveText('研究中');
  expect(state.resumed).toBe(1);
  await expect(page.getByRole('button', { name: '从检查点恢复' })).toHaveCount(0);
});

test('Markdown never executes raw HTML or unsafe source links', async ({ page }) => {
  const state = await backend(page);
  state.reportMarkdown += '\n\n<script>window.injected=true</script>\n\n<img src=x onerror="window.injected=true">\n\n[危险链接](javascript:alert(1))';
  await page.goto('/runs/completed-run/report');
  await expect(page.getByRole('heading', { name: '离线强化学习研究报告' })).toBeVisible();
  await expect(page.locator('.markdown script, .markdown img')).toHaveCount(0);
  await expect(page.getByRole('link', { name: '危险链接' })).toHaveCount(0);
  expect(await page.evaluate(() => (window as Window & { injected?: boolean }).injected)).toBeUndefined();
});

test('polling errors keep the latest real status visible', async ({ page }) => {
  const state = await backend(page);
  await page.goto('/runs/running-run');
  await expect(page.locator('.run-heading .status')).toHaveText('研究中');
  state.statusError = true;
  await expect(page.getByRole('alert')).toContainText('服务暂时无法处理请求', { timeout: 8000 });
  await expect(page.locator('.run-heading .status')).toHaveText('研究中');
});

test('desktop and narrow layouts stay within the viewport', async ({ page }) => {
  await backend(page);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: '最近的研究' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/dashboard-${width}.png`, fullPage: true });
  }
});

async function fakeStream(page: Page) {
  await page.addInitScript(() => {
    type FakeWindow = Window & { __streams?: Fake[]; __emit?: (data: unknown) => void; __disconnect?: () => void };
    const target = window as FakeWindow;
    class Fake extends EventTarget {
      onopen: ((event: Event) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      closed = false;
      constructor(public url: string) {
        super(); (target.__streams ||= []).push(this);
        setTimeout(() => { if (!this.closed) this.onopen?.(new Event('open')); }, 0);
      }
      close() { this.closed = true; }
    }
    window.EventSource = Fake as unknown as typeof EventSource;
    target.__emit = (data) => {
      const item = data as { event_type: string };
      target.__streams?.at(-1)?.dispatchEvent(new MessageEvent(item.event_type, { data: JSON.stringify(data) }));
    };
    target.__disconnect = () => target.__streams?.at(-1)?.onerror?.(new Event('error'));
  });
}

function wire(type: ResearchEvent['event_type'], data: ResearchEvent['progress_data'] = {}, task: string | null = null): ResearchEvent {
  const messages: Partial<Record<ResearchEvent['event_type'], string>> = {
    task_started: '任务开始执行', verification_started: '开始验证任务结论', task_completed: '任务与必要验证已完成并保存',
    run_completed: '研究已完成', run_failed: '研究执行失败，请查询已保存状态',
  };
  return { run_id: 'running-run', event_id: `event-${Math.random()}`, event_type: type,
    timestamp: new Date().toISOString(), task_id: task, stage: 'INITIAL_RESEARCH_BATCH', message: messages[type] || '研究阶段已更新', progress_data: data };
}

async function emit(page: Page, event: ResearchEvent) {
  await page.evaluate(data => (window as Window & { __emit?: (data: unknown) => void }).__emit?.(data), event);
}

test('SSE task updates reach cards and timeline before the checkpoint changes', async ({ page }) => {
  await backend(page); await fakeStream(page);
  await page.goto('/runs/running-run');
  await expect(page.getByText('实时进度已连接', { exact: true })).toBeVisible();
  await emit(page, wire('task_started', { task_status: 'running' }, 'A'));
  await expect(page.locator('#task-A .status')).toHaveText('执行中');
  await expect(page.locator('.execution-timeline')).toContainText('任务开始执行');
  await emit(page, wire('verification_started', { task_status: 'running', phase: 'verification', phase_status: 'running' }, 'A'));
  await expect(page.locator('.phase-progress-item').filter({ hasText: '结论验证' })).toContainText('进行中');
  await page.screenshot({ path: 'test-results/streaming-progress.png', fullPage: true });
});

test('SSE reconnect heals from snapshot and does not POST any research again', async ({ page }) => {
  const state = await backend(page); await fakeStream(page);
  await page.goto('/runs/running-run');
  await expect(page.getByText('实时进度已连接', { exact: true })).toBeVisible();
  const reads = state.runReads;
  await page.evaluate(() => (window as Window & { __disconnect?: () => void }).__disconnect?.());
  await expect(page.getByText('实时连接重试中 · 状态查询继续')).toBeVisible();
  await expect(page.getByText('实时进度已连接', { exact: true })).toBeVisible({ timeout: 5000 });
  await emit(page, wire('run_snapshot', { run_status: 'RUNNING', tasks: [{ task_id: 'A', status: 'running' }] }));
  await expect(page.locator('#task-A .status')).toHaveText('执行中');
  expect(state.runReads).toBeGreaterThan(reads);
  expect(state.created).toHaveLength(0);
  expect(state.resumed).toBe(0);
});

test('completion event confirms SQLite state before automatically opening the saved report', async ({ page }) => {
  const state = await backend(page); await fakeStream(page);
  await page.goto('/runs/running-run');
  await expect(page.getByText('实时进度已连接', { exact: true })).toBeVisible();
  state.complete = true;
  await emit(page, wire('run_completed', { run_status: 'COMPLETED' }));
  await expect(page).toHaveURL('/runs/running-run/report');
  await expect(page.getByRole('heading', { name: '离线强化学习研究报告' })).toBeVisible();
  await expect(page.getByText('已同步保存状态', { exact: true })).toBeVisible();
  // React StrictMode can repeat a read-only mount request in development.
  expect(state.reportReads).toBeGreaterThan(0);
  expect(state.created).toHaveLength(0);
  expect(state.resumed).toBe(0);
});
