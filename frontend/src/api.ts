import type { EvidenceGraph, ResearchRun, RunAccepted, SavedReport } from './types';

const base = (import.meta.env.VITE_API_BASE_URL || '/api').replace(/\/+$/, '');

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

const messages: Record<string, string> = {
  RUN_NOT_FOUND: '没有找到这项研究，请检查 Run ID 或返回工作台。',
  RUN_CAPACITY_EXCEEDED: '研究席位已满，请等待当前研究结束后再试。',
  RUN_ALREADY_RUNNING: '这项研究正在运行，请查看当前进度。',
  RUN_ALREADY_COMPLETED: '这项研究已经完成，可以直接阅读已保存的报告。',
  REPORT_NOT_READY: '研究报告尚未完成，请稍后查看。',
  REPORT_UNAVAILABLE: '这项研究没有保存可读取的报告。',
  CHECKPOINT_INVALID: '保存的检查点无法读取，请检查后端运行记录。',
  CHECKPOINT_UNAVAILABLE: '这项研究尚无可用的检查点。',
  INVALID_REQUEST: '提交内容不符合要求，请检查后重试。',
  SERVER_STOPPING: '研究服务正在关闭，请稍后重试。',
  BACKGROUND_START_FAILED: '研究未能启动，已保存的任务可在历史记录中查看。',
  INTERNAL_ERROR: '服务暂时无法处理请求，请稍后重试。',
};

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const timeout = AbortSignal.timeout(15000);
  const signal = init.signal ? AbortSignal.any([init.signal, timeout]) : timeout;
  try {
    const response = await fetch(base + path, {
      ...init, signal, headers: { Accept: 'application/json', ...(init.body ? { 'Content-Type': 'application/json' } : {}) },
    });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      const code = typeof body?.error?.code === 'string' ? body.error.code : 'HTTP_ERROR';
      // Never render proxy HTML or unknown exception text as an API error.
      throw new ApiError(response.status, code, messages[code] || `服务请求失败（HTTP ${response.status}），请稍后重试。`);
    }
    if (!body || typeof body !== 'object') throw new ApiError(502, 'INVALID_RESPONSE', '服务返回了无法识别的数据，请检查 API 地址。');
    return body as T;
  } catch (error) {
    if (init.signal?.aborted) throw error;
    if (error instanceof ApiError) throw error;
    if (timeout.aborted) throw new ApiError(0, 'REQUEST_TIMEOUT', init.method === 'POST'
      ? '提交结果尚未确认，请先刷新最近研究，避免重复提交。' : '请求超时，请重试。');
    throw new ApiError(0, 'NETWORK_ERROR', '无法连接研究服务，请确认后端已启动。');
  }
}

const runPath = (id: string) => `/runs/${encodeURIComponent(id)}`;
export const api = {
  health: (signal?: AbortSignal) => request<{ status: 'ok' }>('/health', { signal }),
  runs: (signal?: AbortSignal) => request<{ runs: ResearchRun[] }>('/runs?limit=20', { signal }),
  create: (question: string) => request<RunAccepted>('/runs', { method: 'POST', body: JSON.stringify({ question }) }),
  run: (id: string, signal?: AbortSignal) => request<ResearchRun>(runPath(id), { signal }),
  resume: (id: string) => request<RunAccepted>(runPath(id) + '/resume', { method: 'POST' }),
  report: (id: string, signal?: AbortSignal) => request<SavedReport>(runPath(id) + '/report', { signal }),
  evidence: (id: string, signal?: AbortSignal) => request<EvidenceGraph>(runPath(id) + '/evidence', { signal }),
  eventsUrl: (id: string) => base + runPath(id) + '/events',
};

export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。';

export function safeHttpUrl(value?: string | null): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    if (['https:', 'http:'].includes(url.protocol) && !url.username && !url.password) return value;
  } catch { /* Unsafe or relative URLs stay as plain text. */ }
  return undefined;
}
