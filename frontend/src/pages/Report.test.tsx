import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SafeMarkdown } from './Report';

describe('saved Markdown', () => {
  it('preserves citations and source links without executing HTML or loading images', () => {
    const markdown = '# 研究报告\n\n研究结论 [3][7]\n\n<script>alert(1)</script>\n\n![tracker](https://evil.example/pixel)\n\n[unsafe](javascript:alert(1))\n\n## References\n\n[3] 来源 https://example.org/paper';
    const { container } = render(<SafeMarkdown markdown={markdown} />);
    expect(screen.getByRole('heading', { name: '研究报告' })).toBeInTheDocument();
    expect(screen.getByText('研究结论 [3][7]')).toBeInTheDocument();
    expect(container.querySelector('script, img, iframe')).toBeNull();
    expect(screen.queryByRole('link', { name: 'unsafe' })).not.toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'https://example.org/paper' });
    expect(link).toHaveAttribute('href', 'https://example.org/paper');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });
  it('supports report tables and preserves code as text', () => {
    const { container } = render(<SafeMarkdown markdown={'| 方法 | 结果 |\n| --- | --- |\n| A | 已记录 |\n\n`<script>text</script>`'} />);
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(container.querySelector('code')).toHaveTextContent('<script>text</script>');
  });
});
