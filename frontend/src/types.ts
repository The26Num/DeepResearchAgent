export type RunStatus = 'RUNNING' | 'FAILED' | 'INTERRUPTED' | 'COMPLETED';
export type TaskStatus = 'pending' | 'running' | 'completed' | 'failed';
export type Phase = 'planning' | 'research' | 'verification' | 'gap_detection' | 'reresearch' | 'writer' | 'citation';
export type PhaseStatus = 'pending' | 'running' | 'completed' | 'failed' | 'interrupted' | 'skipped';
export interface PhaseProgress { phase: Phase; status: PhaseStatus }
export interface ApiErrorDetail { code: string; message: string }
export interface ResearchTask {
  id: string; title: string; question: string; description: string;
  task_type: 'discovery' | 'analysis' | 'synthesis'; depends_on: string[]; status: TaskStatus;
}
export interface ResearchRun {
  run_id: string; question: string; status: RunStatus; current_stage: string;
  created_at: string; updated_at: string; resume_count: number;
  checkpoint_available: boolean; resumable: boolean;
  plan: { goal: string; tasks: ResearchTask[] } | null;
  supplemental_tasks: ResearchTask[]; error: ApiErrorDetail | null;
  phase_statuses?: PhaseProgress[];
}
export interface RunAccepted { run_id: string; status: RunStatus }
export interface Source {
  source_id: string; task_id: string; title: string; url: string;
  source_type: 'paper' | 'webpage' | 'repository' | 'unknown'; published_at: string | null;
}
export interface Evidence {
  evidence_id: string; source_id: string; task_id: string; content: string; location: string | null;
}
export interface Claim { claim_id: string; task_id: string; text: string; evidence_ids: string[] }
export interface Verification {
  verification_id: string; claim_id: string; task_id: string;
  status: 'SUPPORTED' | 'PARTIAL' | 'UNSUPPORTED'; reason: string; evidence_ids: string[];
}
export interface Relevance {
  relevance_id: string; claim_id: string; task_id: string;
  status: 'ALIGNED' | 'PARTIAL' | 'MISALIGNED'; reason: string;
}
export interface EvidenceGraph {
  run_id: string; checkpoint_available: boolean; checkpoint_stage: string | null;
  claims: Claim[]; evidence: Evidence[]; sources: Source[];
  verification_results: Verification[]; relevance_results: Relevance[];
}
export interface ReportParagraph { text: string; claim_ids: string[] }
export interface Reference {
  citation_number: number; source_id: string; title: string; url: string;
  source_type: string; published_at: string | null;
}
export interface ResearchReport {
  title: string; scope: string; executive_summary: ReportParagraph[]; key_findings: ReportParagraph[];
  sections: { heading: string; paragraphs: ReportParagraph[] }[]; conclusion: ReportParagraph[];
  limitations: { text: string; gap_ids: string[]; partial_claim_ids: string[] }[];
}
export interface SavedReport {
  run_id: string; status: 'COMPLETED'; report: ResearchReport; markdown: string; references: Reference[];
}

export const eventTypes = [
  'run_started', 'run_snapshot', 'stage_changed', 'plan_created', 'task_started', 'task_research_completed',
  'task_completed', 'task_failed', 'verification_started', 'verification_completed', 'verification_failed',
  'initial_research_completed', 'gap_detection_started', 'gap_detection_completed', 'reresearch_started',
  'reresearch_plan_created', 'reresearch_completed', 'writer_started', 'writer_completed', 'citation_started',
  'report_completed', 'run_failed', 'run_interrupted', 'run_completed', 'stream_reset', 'stream_error',
] as const;
export type EventType = typeof eventTypes[number];
export interface ResearchEvent {
  run_id: string; event_id: string; event_type: EventType; timestamp: string;
  task_id: string | null; stage: string; message: string;
  progress_data: {
    run_status?: RunStatus | null; task_status?: TaskStatus | null;
    phase?: Phase | null; phase_status?: PhaseStatus | null;
    elapsed_seconds?: number | null; resume_count?: number | null;
    task_count?: number | null; completed_tasks?: number | null;
    tasks?: { task_id: string; status: TaskStatus }[]; phases?: PhaseProgress[];
  };
}
