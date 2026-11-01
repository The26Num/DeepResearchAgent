import type { EvidenceGraph, ResearchRun, SavedReport } from '../src/types';

// Test-only records. None are imported by the application or included in dist.
export const goal = '调研离线强化学习的最新研究进展';
const tasks: ResearchRun['plan'] = { goal, tasks: [
  { id: 'D1', title: '代表性方法研究', question: '调研离线强化学习的代表性方法', description: '阅读原始研究来源', task_type: 'discovery', depends_on: [], status: 'completed' },
  { id: 'D2', title: '当前挑战与趋势', question: '调研离线强化学习的挑战与趋势', description: '寻找研究局限', task_type: 'discovery', depends_on: [], status: 'running' },
  { id: 'A', title: '综合分析研究进展', question: '分析已有研究结果', description: '比较两项资料', task_type: 'analysis', depends_on: ['D1', 'D2'], status: 'pending' },
  { id: 'S', title: '汇总研究结论', question: '综合离线强化学习的研究进展', description: '汇总已有研究', task_type: 'synthesis', depends_on: ['A'], status: 'pending' },
] };
export const running: ResearchRun = { run_id: 'running-run', question: goal, status: 'RUNNING', current_stage: 'INITIAL_RESEARCH_BATCH',
  created_at: '2026-10-16T08:00:00+00:00', updated_at: '2026-10-16T08:02:00+00:00', resume_count: 0,
  checkpoint_available: true, resumable: false, plan: tasks, supplemental_tasks: [], error: null };
export const completed: ResearchRun = { ...running, run_id: 'completed-run', question: '离线强化学习的研究方向、方法与挑战', status: 'COMPLETED', current_stage: 'RUN_COMPLETED',
  plan: { goal, tasks: tasks.tasks.map(task => ({ ...task, status: 'completed' })) } };
export const failed: ResearchRun = { ...running, run_id: 'failed-run', question: '研究数据集的比较与分析', status: 'FAILED', resumable: true,
  error: { code: 'RESEARCH_TIMEOUT', message: 'Request timed out.' } };
export const graph: EvidenceGraph = { run_id: 'completed-run', checkpoint_available: true, checkpoint_stage: 'RUN_COMPLETED',
  claims: [
    { claim_id: 'claim_001', task_id: 'D1', text: '离线强化学习依赖既有数据进行策略学习。', evidence_ids: ['ev_001'] },
    { claim_id: 'claim_002', task_id: 'D2', text: '数据覆盖范围是研究中需要考虑的因素。', evidence_ids: ['ev_002'] },
  ],
  evidence: [
    { evidence_id: 'ev_001', source_id: 'src_001', task_id: 'D1', content: 'Offline reinforcement learning learns from previously collected data.', location: 'Abstract' },
    { evidence_id: 'ev_002', source_id: 'src_002', task_id: 'D2', content: 'The coverage of the dataset affects the available learning signal.', location: null },
  ],
  sources: [
    { source_id: 'src_001', task_id: 'D1', title: 'Offline Reinforcement Learning: Research Material', url: 'https://arxiv.org/abs/2401.00001', source_type: 'paper', published_at: '2024' },
    { source_id: 'src_002', task_id: 'D2', title: 'Dataset Coverage: Research Material', url: 'https://example.org/dataset', source_type: 'webpage', published_at: null },
  ],
  verification_results: [{ verification_id: 'v_001', claim_id: 'claim_001', task_id: 'D1', status: 'SUPPORTED', reason: '来源原文直接支持该表述。', evidence_ids: ['ev_001'] }],
  relevance_results: [{ relevance_id: 'r_001', claim_id: 'claim_001', task_id: 'D1', status: 'ALIGNED', reason: '与研究目标一致。' }],
};
export const report: SavedReport = { run_id: 'completed-run', status: 'COMPLETED',
  report: { title: '离线强化学习研究报告', scope: '研究范围与现有材料', executive_summary: [{ text: graph.claims[0].text, claim_ids: ['claim_001'] }],
    key_findings: [{ text: graph.claims[0].text, claim_ids: ['claim_001'] }], sections: [{ heading: '数据与策略学习', paragraphs: [{ text: graph.claims[0].text, claim_ids: ['claim_001'] }] }],
    limitations: [{ text: '现有资料尚不足以覆盖全部研究方向。', gap_ids: [], partial_claim_ids: [] }], conclusion: [{ text: graph.claims[0].text, claim_ids: ['claim_001'] }] },
  markdown: '# 离线强化学习研究报告\n\n## 执行摘要\n\n离线强化学习依赖既有数据进行策略学习。 [1]\n\n## 研究范围\n\n研究范围与现有材料\n\n## 主要发现\n\n- 离线强化学习依赖既有数据进行策略学习。 [1]\n\n## 详细分析\n\n### 数据与策略学习\n\n离线强化学习依赖既有数据进行策略学习。 [1]\n\n## 局限性与未解决问题\n\n- 现有资料尚不足以覆盖全部研究方向。\n\n## 结论\n\n离线强化学习依赖既有数据进行策略学习。 [1]\n\n## References\n\n[1] Offline Reinforcement Learning: Research Material. 2024. https://arxiv.org/abs/2401.00001',
  references: [{ citation_number: 1, source_id: 'src_001', title: graph.sources[0].title, url: graph.sources[0].url, source_type: 'paper', published_at: '2024' }],
};
