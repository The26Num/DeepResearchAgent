"""Sequential benchmark cases using the normal persisted research entry point."""

from datetime import datetime, timezone
from pathlib import Path
import re
from time import monotonic

from app.config import get_settings
from app.persistence.store import ResearchRunStore
from app.research.orchestrator import ResearchOrchestrator
from evaluation.baselines.simple_search_summary import SimpleSearchSummary
from evaluation.dataset import load_benchmark, select_cases
from evaluation.judge import ReportJudge, EvaluationSettings, full_judge_sources, baseline_judge_sources
from evaluation.metrics import Evaluator, baseline_citation_metrics, duplication, efficiency_metrics
from evaluation.models import EvaluationError, EvaluationResult, MetricVector
from evaluation.output import EvaluationOutput, Redactor, git_commit
from evaluation.telemetry import observe_execution


class TrackingRunStore(ResearchRunStore):
    """Capture the public Run ID even when run() raises before returning a result."""
    created_run_id = None

    def create_run(self, goal):
        run = super().create_run(goal)
        self.created_run_id = run.run_id
        return run


def agent_config(settings, max_concurrency):
    # Explicit allowlist. Do not serialize Settings, environment or provider clients.
    names = ("model_name", "verifier_model_name", "model_request_timeout_seconds", "model_max_retries",
             "research_task_timeout_seconds", "evidence_extraction_timeout_seconds", "evidence_extraction_max_tokens",
             "evidence_extraction_enable_thinking", "max_verify_claims_per_batch", "max_verification_context_chars",
             "verification_max_tokens", "verification_timeout_seconds", "verifier_enable_thinking",
             "max_relevance_claims_per_batch", "max_relevance_context_chars", "relevance_max_tokens", "relevance_timeout_seconds",
             "max_reresearch_rounds", "max_reresearch_tasks_per_round", "max_reresearch_gaps_per_task", "max_reresearch_llm_calls",
             "reresearch_max_search_calls", "reresearch_max_fetch_calls", "max_reresearch_planning_context_chars",
             "reresearch_planning_max_tokens", "reresearch_planning_timeout_seconds", "max_writer_context_chars",
             "max_report_output_tokens", "max_evidence_per_writing_claim", "max_writing_evidence_chars",
             "min_writer_claim_coverage", "writer_timeout_seconds", "writer_enable_thinking",
             "max_report_grounding_context_chars", "report_grounding_max_tokens", "report_grounding_timeout_seconds")
    from app.research.tool_budget import ToolBudget
    from app.schemas.task import ResearchTask
    budgets = {}
    for kind in ("discovery", "analysis", "synthesis"):
        budget = ToolBudget.for_task(ResearchTask(id="config", title="config", question="config", description="config", task_type=kind))
        budgets[kind] = {"search": budget.max_search_calls, "fetch": budget.max_fetch_calls}
    return {**{name: getattr(settings, name) for name in names}, "max_concurrency": max_concurrency,
            "temperature": 0, "original_task_tool_budgets": budgets}


class EvaluationRunner:
    def __init__(self, *, settings=None, output_root="workspace/evaluations", db_path=None, max_concurrency=3,
                 judge_enabled=False, orchestrator_factory=None, baseline_factory=None, judge_factory=None):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive.")
        self.settings = settings or get_settings()
        self.output_root, self.db_path = output_root, db_path or self.settings.research_db_path
        self.max_concurrency, self.judge_enabled = max_concurrency, judge_enabled
        self.orchestrator_factory = orchestrator_factory or (
            lambda store, concurrency: ResearchOrchestrator(run_store=store, max_concurrency=concurrency))
        self.baseline_factory = baseline_factory or (lambda: SimpleSearchSummary(max_output_tokens=self.settings.max_report_output_tokens))
        self.judge_factory = judge_factory or ReportJudge
        self.evaluator = Evaluator()
        self.output = None

    def run(self, benchmark_path, *, mode="full", case_id=None, limit=None):
        if mode not in {"full", "baseline", "compare"}:
            raise ValueError("Unknown evaluation mode.")
        dataset = load_benchmark(benchmark_path)
        cases = select_cases(dataset, case_id=case_id, limit=limit)
        self.output = EvaluationOutput(self.output_root, redactor=Redactor(self.settings))
        print(f"Evaluation ID: {self.output.eval_id}\nCases selected: {len(cases)}; mode={mode}; judge={self.judge_enabled}", flush=True)
        self.output.save_json("config.json", {"eval_id": self.output.eval_id, "benchmark_path": str(Path(benchmark_path).resolve()),
            "benchmark_version": dataset.benchmark_version, "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": mode, "case_ids": [item.case_id for item in cases], "cases_requested": len(cases),
            "model_name": self.settings.model_name, "agent_config": agent_config(self.settings, self.max_concurrency),
            "judge_enabled": self.judge_enabled, "judge_model_name": EvaluationSettings().eval_model_name or self.settings.model_name,
            "judge_max_calls_per_report": 1, "baseline_tool_budgets": {"search": 3, "fetch": 4},
            "research_db_path": str(Path(self.db_path).resolve()), "git_commit": git_commit(),
            "metric_version": 1, "judge_rubric_version": 1})
        self.output.save_json("benchmark.json", dataset.model_dump(mode="json"))
        self.output.summarize()
        for index, case in enumerate(cases, 1):
            print(f"[Evaluation] case {index}/{len(cases)}: {case.case_id}", flush=True)
            results = []
            if mode in {"full", "compare"}:
                results.append(self._full(case, dataset.benchmark_version))
            if mode in {"baseline", "compare"} and not any(item.status == "INTERRUPTED" for item in results):
                results.append(self._baseline(case, dataset.benchmark_version))
            self.output.append_case(case, results)
            for result in results:
                print(f"[Evaluation] {case.case_id} {result.variant}: {result.status}; run_id={result.run_id or 'N/A'}", flush=True)
            if any(item.status == "INTERRUPTED" for item in results):
                raise KeyboardInterrupt
        summary = self.output.summarize()
        self.print_summary(summary)
        return summary

    def _error(self, stage, error):
        return EvaluationError(stage=stage, error_type=type(error).__name__,
                               error_message=self.output.redactor.clean(str(error)))

    def _full(self, case, version):
        result = EvaluationResult(case_id=case.case_id, benchmark_version=version, variant="full", status="COMPLETED")
        started, store, telemetry, state = monotonic(), None, None, None
        try:
            store = TrackingRunStore(self.db_path)
            with observe_execution() as collector:
                try:
                    engine = self.orchestrator_factory(store, self.max_concurrency)
                    run_result = engine.run(case.question)  # Normal Phase 6 API; no internal stage calls.
                    result.run_id = run_result.run_id
                finally:
                    telemetry = collector.snapshot()
        except KeyboardInterrupt as error:
            result.status = "INTERRUPTED"
            result.errors.append(self._error("research", error))
        except Exception as error:
            result.status = "FAILED"
            result.errors.append(self._error("research", error))
        elapsed = monotonic() - started
        result.metrics = MetricVector(efficiency=efficiency_metrics(telemetry), timing={"total_runtime_seconds": elapsed})
        result.run_id = result.run_id or (store.created_run_id if store else None)
        try:
            if result.run_id:
                run = store.get_run(result.run_id)
                state = store.load_latest_checkpoint(result.run_id).state
                with store.connection() as connection:
                    count = connection.execute("SELECT count(*) FROM checkpoints WHERE run_id=?", (result.run_id,)).fetchone()[0]
                result.metrics = self.evaluator.evaluate(state, case, run=run, checkpoint_count=count,
                                                         telemetry=telemetry, runtime_seconds=elapsed)
                if run.status != "COMPLETED" and result.status == "COMPLETED":
                    raise RuntimeError(f"Research did not complete: {run.status.value}.")
                if state.rendered_report:
                    result.report_path = self.output.save_report(case.case_id, "full", state.rendered_report.markdown)
            else:
                result.metrics = MetricVector(efficiency=efficiency_metrics(telemetry), timing={"total_runtime_seconds": elapsed})
        except Exception as error:
            if result.status == "COMPLETED":
                result.status = "FAILED"
            result.errors.append(self._error("evaluation", error))
        if self.judge_enabled and result.status == "COMPLETED" and state and state.rendered_report:
            self._judge(result, case, state.rendered_report.markdown, full_judge_sources(state))
        return result

    def _baseline(self, case, version):
        result = EvaluationResult(case_id=case.case_id, benchmark_version=version, variant="baseline", status="COMPLETED")
        started, telemetry = monotonic(), None
        try:
            with observe_execution() as collector:
                try:
                    baseline = self.baseline_factory().run(case.question)
                finally:
                    telemetry = collector.snapshot()
            duplicates, duplicate_rate = duplication([item for item in baseline.report.split("\n\n")
                                                      if item.strip() and not item.strip().startswith("#")])
            quality = {**baseline_citation_metrics(baseline.report, baseline.sources),
                       "support_rate": None, "alignment_rate": None, "publishable_claim_rate": None,
                       "task_coverage_rate": None, "writer_claim_coverage": None, "gap_repair_rate": None,
                       "duplicate_paragraph_count": duplicates, "duplicate_paragraph_rate": duplicate_rate,
                       "report_chars": len(baseline.report), "report_words": len(re.findall(r"\b\w+\b", baseline.report))}
            result.metrics = MetricVector(quality=quality, efficiency=efficiency_metrics(telemetry),
                research={"stored_sources": len(baseline.sources), "cited_sources": quality["cited_sources"]},
                timing={"total_runtime_seconds": monotonic() - started})
            result.report_path = self.output.save_report(case.case_id, "baseline", baseline.report)
            self.output.save_json(f"{case.case_id}.baseline.json", baseline.model_dump(mode="json"))
            if self.judge_enabled:
                self._judge(result, case, baseline.report, baseline_judge_sources(baseline))
        except KeyboardInterrupt as error:
            result.status = "INTERRUPTED"
            result.errors.append(self._error("baseline", error))
        except Exception as error:
            result.status = "FAILED"
            result.errors.append(self._error("baseline", error))
        if result.status != "COMPLETED":
            result.metrics.efficiency = efficiency_metrics(telemetry)
            result.metrics.timing = {"total_runtime_seconds": monotonic() - started}
        return result

    def _judge(self, result, case, report, sources):
        judge = None
        try:
            judge = self.judge_factory()  # One fresh budget per report, no retries.
            result.judge_result = judge.evaluate(case, report, sources)
        except Exception as error:
            # A judge failure is diagnostic; it never edits the report or reruns research.
            result.errors.append(self._error("judge", error))
        finally:
            result.judge_usage = dict(judge.last_usage) if judge else {"llm_calls": 0}

    def print_summary(self, summary):
        print("=" * 40 + "\nEVALUATION SUMMARY\n" + "=" * 40)
        print(f"Cases: {summary['cases_total']}  Completed: {summary['cases_completed']}  Failed: {summary['cases_failed']}")
        for variant, values in summary["variants"].items():
            print(f"\n{variant}:")
            names = {"Support rate": "quality.support_rate", "Alignment rate": "quality.alignment_rate",
                     "Publishable rate": "quality.publishable_claim_rate", "Task coverage": "quality.task_coverage_rate",
                     "Citation integrity": "quality.citation_integrity_pass", "LLM calls": "efficiency.total_llm_calls",
                     "Tokens (complete usage only)": "efficiency.total_tokens", "Search calls": "efficiency.web_search_calls",
                     "Runtime seconds": "timing.total_runtime_seconds", "Goal relevance": "judge.goal_relevance",
                     "Coverage": "judge.coverage", "Groundedness": "judge.groundedness", "Clarity": "judge.clarity"}
            for label, key in names.items():
                value = values["means"].get(key)
                display = f"{value:.3f}" if value is not None else "N/A"
                print(f"  {label}: {display} (n={values['metric_sample_count'].get(key, 0)})")
        print(f"\nResults: {self.output.path}")
