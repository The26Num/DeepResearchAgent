import { describe, expect, it, vi } from 'vitest';
import { api, ApiError, safeHttpUrl } from './api';

describe('central API client', () => {
  it('sends only the question and never retries a write automatically', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'RUN_CAPACITY_EXCEEDED' } }), { status: 429 }));
    vi.stubGlobal('fetch', fetch);
    await expect(api.create('研究问题')).rejects.toMatchObject({ status: 429, code: 'RUN_CAPACITY_EXCEEDED' });
    expect(fetch).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe('/api/runs');
    expect(JSON.parse(init.body)).toEqual({ question: '研究问题' });
    expect(init.headers).toEqual({ Accept: 'application/json', 'Content-Type': 'application/json' });
  });
  it('does not expose raw proxy HTML or server exception text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<h1>C:/private/key.env</h1>', { status: 502 })));
    await expect(api.runs()).rejects.toThrow('HTTP 502');
  });
  it('encodes run IDs as one path segment', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetch);
    await api.run('id/with spaces');
    expect(fetch.mock.calls[0][0]).toBe('/api/runs/id%2Fwith%20spaces');
  });
  it('reports a disconnected server as a readable network error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('failed to fetch')));
    await expect(api.health()).rejects.toBeInstanceOf(ApiError);
    await expect(api.health()).rejects.toThrow('无法连接研究服务');
  });
});

describe('source URL safety', () => {
  it.each(['javascript:alert(1)', 'data:text/html,hello', 'file:///C:/private/key.env', '//evil.example', '/relative', 'https://user:password@example.org'])('does not activate %s', value => {
    expect(safeHttpUrl(value)).toBeUndefined();
  });
  it('retains the original safe URL', () => {
    const value = 'https://arxiv.org/abs/2401.00001?version=1';
    expect(safeHttpUrl(value)).toBe(value);
  });
});
