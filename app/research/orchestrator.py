import asyncio
from collections import Counter
from dataclasses import dataclass, field

from app.agents.researcher import Researcher
from app.agents.supervisor import Supervisor
from app.agents.verifier import ClaimVerifier, VerificationUsage
from app.agents.relevance import RelevanceUsage
from app.agents.reresearch_planner import ReResearchPlanner
from app.agents.writer import ResearchWriter, WriterUsage
from app.agents.report_grounding import ReportGroundingAudit
from app.llm import create_llm
from app.config import get_settings
from app.research.context import build_dependency_context
from app.research.evidence_store import EvidenceStore
from app.research.evidence_context import EvidenceContext, build_evidence_context
from app.research.evidence_report import finalize_evidence_report
from app.research.verification_store import VerificationStore
from app.research.gap_detection import GapDetector
from app.research.gap_store import GapStore
from app.research.reresearch import ReResearchSummary, select_gap_groups, update_group_status
from app.research.reresearch_budget import ReResearchCallBudget, ReResearchBudgetExhausted, use_call_budget
from app.schemas.evidence_bundle import TaskEvidenceBundle
from app.schemas.plan import ResearchPlan, validate_plan_dependencies
from app.schemas.research_result import CompactResearchResult, ResearchExecutionResult
from app.schemas.task import ResearchTask
from app.schemas.relevance import ClaimRelevanceResult
from app.schemas.reresearch import ReResearchPlan
from app.schemas.report import ResearchReport
from app.research.writing_context import WritingContext, WritingContextBuilder
from app.research.report_rendering import render_report_with_citations
from app.research.citations import CitationIntegrityError
from app.schemas.citation import CitationUsage, RenderedResearchReport


@dataclass
class ResearchResult:
    plan: ResearchPlan #Supervisor一开始生成的研究计划
    memos: dict[str, str] #给用户看
    compact_results: dict[str, CompactResearchResult]# 给task2看
    evidence_store: EvidenceStore = field(default_factory=EvidenceStore)
    evidence_bundles: dict[str, TaskEvidenceBundle] = field(default_factory=dict)
    verification_store: VerificationStore = field(default_factory=VerificationStore)
    verification_usage: VerificationUsage = field(default_factory=VerificationUsage)
    relevance_results: dict[str, ClaimRelevanceResult] = field(default_factory=dict)
    relevance_usage: RelevanceUsage = field(default_factory=RelevanceUsage)
    gap_store: GapStore = field(default_factory=GapStore)
    reresearch_summary: ReResearchSummary = field(default_factory=ReResearchSummary)
    supplemental_results: dict[str, ResearchExecutionResult] = field(default_factory=dict)
    report: ResearchReport | None = None
    writer_usage: WriterUsage = field(default_factory=WriterUsage)
    writing_context: WritingContext | None = None
    report_grounding: tuple[ReportGroundingAudit, ...] = ()
    rendered_report: RenderedResearchReport | None = None
    citation_usage: CitationUsage = field(default_factory=CitationUsage)


class ResearchOrchestrator:
    def __init__(
        self, supervisor: Supervisor | None = None, researcher: Researcher | None = None,
        *, max_concurrency: int = 3, verifier: ClaimVerifier | None = None,
        verify_claims: bool = True,
        gap_detector: GapDetector | None = None, detect_gaps: bool | None = None,
        reresearch: bool | None = None, reresearch_planner: ReResearchPlanner | None = None,
        write_report: bool | None = None, writer: ResearchWriter | None = None,
        writing_context_builder: WritingContextBuilder | None = None,
    ) -> None:
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer.")
        if supervisor is None or researcher is None:
            model = create_llm()
            supervisor = supervisor or Supervisor(model)
            researcher = researcher or Researcher(model)
        self.supervisor = supervisor
        self.researcher = researcher
        self.max_concurrency = max_concurrency
        self.verifier = verifier or ClaimVerifier()
        self.verify_claims = verify_claims
        self.detect_gaps = verify_claims if detect_gaps is None else detect_gaps
        if self.detect_gaps and not verify_claims:
            raise ValueError("Gap detection requires Phase 4A verification; enable verify_claims or disable detect_gaps.")
        self.gap_detector = gap_detector or GapDetector()
        self.reresearch = self.detect_gaps if reresearch is None else reresearch
        if self.reresearch and not self.detect_gaps:
            raise ValueError("Re-Research requires gap detection.")
        self.reresearch_planner = reresearch_planner or ReResearchPlanner()
        self.write_report = self.detect_gaps if write_report is None else write_report
        if self.write_report and not self.detect_gaps:
            raise ValueError("Writer requires Claim verification and relevance; enable gap detection or disable write_report.")
        self.writer = writer or ResearchWriter()
        self.writing_context_builder = writing_context_builder or WritingContextBuilder()

    def run(self, query: str) -> ResearchResult:
        """Synchronous entry point for the CLI; async callers should await arun()."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(query))
        raise RuntimeError("An event loop is already running; use await ResearchOrchestrator.arun(query).")

    async def arun(self, query: str) -> ResearchResult:
        plan = await asyncio.to_thread(self.supervisor.create_plan, query)
        validate_plan_dependencies(plan)  #对研究计划进行依赖性验证，确保没有循环依赖或未定义的依赖
        if any(task.status != "pending" for task in plan.tasks):  #Supervisor生成的研究计划中的所有任务状态必须是pending，如果不是则抛出异常
            raise ValueError("ResearchPlan tasks must start as pending.")
        #为什么要求所有任务状态必须是pending？因为在研究计划生成后，所有任务都还没有开始执行。
        #supervisor只负责生成研究计划，而不执行任务。任务的状态在执行过程中会被更新为running、completed或failed。
        #任务状态由ResearchOrchestrator在执行任务时进行管理，以确保任务的执行顺序和依赖关系得到正确处理。
        print("=" * 40 + "\n研究计划\n" + "=" * 40) 
        print(f"目标：{plan.goal}\n")  #这里打印的就是supervisor生成的研究计划
        for task in plan.tasks:
            print(f"[{task.id}] {task.title} — {task.question}")
            print(f"任务类型：{task.task_type}")
            print(f"依赖：{', '.join(task.depends_on) or '无'}\n")

        executions: dict[str, ResearchExecutionResult] = {}
        evidence_store = EvidenceStore()  # One isolated Store per run; workers never receive it.
        verification_store = VerificationStore()
        verification_usage = VerificationUsage()
        while pending_tasks := [task for task in plan.tasks if task.status == "pending"]:
            ready_tasks = self._get_ready_tasks(plan, executions)
            if not ready_tasks:
                remaining = ", ".join(task.id for task in pending_tasks)
                raise RuntimeError(f"No research task is ready; unresolved dependencies for: {remaining}.")

            batch = ready_tasks[:self.max_concurrency]
            print(
                "\n" + "=" * 40
                + f"\n正在执行批次：{len(batch)} 项任务（并发上限 {self.max_concurrency}）\n"
                + "=" * 40,
                flush=True,
            )
            # Wait for every worker in this batch, including when a sibling fails.
            # Cancelling an await on to_thread() cannot stop its synchronous worker.
            outcomes = await asyncio.gather(
                *(self._run_task(task, executions, query, evidence_store,
                                 self._evidence_dependencies(task, plan)) for task in batch),
                return_exceptions=True,
            )
            failures: list[tuple[ResearchTask, BaseException]] = []
            for task, outcome in zip(batch, outcomes):
                if isinstance(outcome, BaseException):
                    failures.append((task, outcome))
                else:
                    try:
                        if outcome.evidence_bundle is not None:
                            if outcome.evidence_bundle.task_id != task.id:
                                raise ValueError("Returned evidence bundle does not belong to the current task.")
                            local = outcome.evidence_bundle
                            before = evidence_store.counts
                            canonical = evidence_store.merge(local)
                            after = evidence_store.counts
                            new_sources = after['sources'] - before['sources']
                            new_evidence = after['evidence'] - before['evidence']
                            reused = {key for claim in canonical.claims for key in claim.evidence_ids
                                      if evidence_store.get_evidence(key).task_id != task.id}
                            print(f"[{task.id}] store merge: new_sources={new_sources} "
                                  f"reused_sources={len(local.sources) - new_sources} "
                                  f"new_evidence={new_evidence} reused_evidence={len(local.evidence) - new_evidence}", flush=True)
                            print(f"[{task.id}] evidence reuse: reused={len(reused)} new={new_evidence}", flush=True)
                            outcome = outcome.model_copy(update={"evidence_bundle": canonical}, deep=True)
                            if isinstance(self.researcher, Researcher) and canonical.claims:
                                # Re-render after deduplication so displayed metadata
                                # and reference labels match the Store's canonical graph.
                                upstream = build_evidence_context(task, evidence_store,
                                                                  self._evidence_dependencies(task, plan))
                                sources = {item.source_id: item for item in upstream.sources}
                                evidence = {item.evidence_id: item for item in upstream.evidence}
                                sources.update((item.source_id, item) for item in evidence_store.get_sources_for_task(task.id))
                                evidence.update((item.evidence_id, item) for item in evidence_store.get_evidence_for_task(task.id))
                                report_context = EvidenceContext(tuple(sources.values()), tuple(evidence.values()), ())
                                memo, compact = finalize_evidence_report(task, canonical, report_context,
                                                                         has_reading=bool(local.evidence))
                                outcome = outcome.model_copy(update={"memo": memo, "compact": compact})
                        executions[task.id] = outcome
                    except Exception as error:
                        task.status = "failed"
                        print(f"[{task.id}] failed — evidence merge: {type(error).__name__}: {error}", flush=True)
                        failures.append((task, error))
            # The existing parallel research batch has completed and all successful
            # bundles are canonical. Verification is serial, with no scheduler rewrite.
            if self.verify_claims:
                for task in batch:
                    if task.id not in executions:
                        continue
                    try:
                        results = await asyncio.to_thread(
                            self.verifier.verify_batch, evidence_store.get_claims_for_task(task.id), evidence_store)
                        for item in results:
                            verification_store.add_verification(item)
                    except Exception as error:
                        print(f"[{task.id}] verification failed — {type(error).__name__}: {error}", flush=True)
                        failures.append((task, error))
                    finally:
                        verification_usage.add(self.verifier.last_usage)
            if failures:
                self._print_results(plan, executions)
                self._print_evidence(plan, evidence_store)
                self._print_verification(verification_store, verification_usage)
                detail = "; ".join(
                    f"{task.id}: {type(error).__name__}: {error}" for task, error in failures
                )
                remaining = ", ".join(task.id for task in plan.tasks if task.status == "pending")
                if remaining:
                    print(f"未执行任务：{remaining}（本批次有任务失败，停止后续调度）", flush=True)
                raise RuntimeError(
                    f"Research tasks failed: {detail}. Pending tasks not executed: {remaining or 'none'}."
                ) from failures[0][1]

        relevance_results = {}
        relevance_usage = RelevanceUsage()
        gap_store = GapStore()
        if self.detect_gaps:
            claims = [claim for task in plan.tasks for claim in evidence_store.get_claims_for_task(task.id)]
            try:
                detection = await asyncio.to_thread(self.gap_detector.detect, plan, claims, verification_store,
                                                     research_goal=query)
                relevance_results, relevance_usage, gap_store = (
                    detection.relevance_results, detection.relevance_usage, detection.gap_store)
            except Exception as error:
                self._print_results(plan, executions)
                self._print_evidence(plan, evidence_store)
                self._print_verification(verification_store, verification_usage)
                usage = self.gap_detector.assessor.last_usage
                print(f"[Gap Detection] failed — {type(error).__name__}: {error}; "
                      f"relevance_calls={usage.llm_calls} context_chars={usage.context_chars}", flush=True)
                raise RuntimeError(f"Gap Detection failed: {type(error).__name__}: {error}.") from error

        supplemental = {}
        rr_summary = ReResearchSummary(initial_gaps=len(gap_store.all_gaps()))
        if self.reresearch:
            supplemental, rr_summary = await self._run_reresearch(
                query, plan, evidence_store, verification_store, verification_usage,
                relevance_results, relevance_usage, gap_store)

        self._print_results(plan, executions)
        self._print_evidence(plan, evidence_store, [task for item in rr_summary.plans for task in item.tasks])
        self._print_verification(verification_store, verification_usage)
        if self.detect_gaps:
            self._print_gaps(relevance_results, relevance_usage, gap_store)
        if self.reresearch:
            self._print_reresearch(rr_summary, gap_store)
        report = None
        writing_context = None
        writer_usage = WriterUsage()
        rendered_report = None
        if self.write_report:
            try:
                writing_context = await asyncio.to_thread(self.writing_context_builder.build, query,
                    evidence_store, verification_store, relevance_results, gap_store, plan=plan)
                report = await asyncio.to_thread(self.writer.write, writing_context)
                writer_usage = self.writer.last_usage
            except Exception as error:
                usage = self.writer.last_usage
                print(f"[Writer] failed — {type(error).__name__}: {error}; calls={usage.llm_calls}", flush=True)
                raise RuntimeError(f"Writer failed: {type(error).__name__}: {error}.") from error
            try:
                rendered_report = await asyncio.to_thread(render_report_with_citations, report, evidence_store,
                                                          debug=get_settings().show_writer_debug)
            except CitationIntegrityError as error:
                print(f"[Citation] failed — {error}; llm_calls=0", flush=True)
                raise RuntimeError(f"Citation failed: {error}") from error
            print("\n" + "=" * 40 + "\nFINAL RESEARCH REPORT\n" + "=" * 40, flush=True)
            print(rendered_report.markdown, flush=True)
        return ResearchResult(
            plan=plan,
            memos={task.id: executions[task.id].memo for task in plan.tasks},
            compact_results={task.id: executions[task.id].compact for task in plan.tasks},
            evidence_store=evidence_store,
            evidence_bundles={key: item.evidence_bundle for key, item in {**executions, **supplemental}.items()
                              if item.evidence_bundle is not None},
            verification_store=verification_store,
            verification_usage=verification_usage,
            relevance_results=relevance_results,
            relevance_usage=relevance_usage,
            gap_store=gap_store,
            reresearch_summary=rr_summary,
            supplemental_results=supplemental,
            report=report,
            writer_usage=writer_usage,
            writing_context=writing_context,
            report_grounding=self.writer.last_grounding if self.write_report else (),
            rendered_report=rendered_report,
            citation_usage=rendered_report.usage if rendered_report else CitationUsage(),
        )

    async def _run_reresearch(self, query, plan, store, verified, verification_usage,
                            relevance_results, relevance_usage, gaps):
        settings = get_settings()
        summary = ReResearchSummary(initial_gaps=len(gaps.all_gaps()))
        supplemental = {}
        if not settings.max_reresearch_rounds:
            return supplemental, summary
        groups = select_gap_groups(gaps.all_gaps(), settings)
        if not groups:
            summary.stop_reason = "no unresolved gaps"
            return supplemental, summary
        summary.selected_groups = len(groups)
        budget = ReResearchCallBudget(settings.max_reresearch_llm_calls)
        before = store.counts
        summary.stop_reason = "round limit reached"
        print("\n" + "=" * 40 + "\nRE-RESEARCH ROUND 1\n" + "=" * 40, flush=True)
        print(f"Open gaps: {summary.initial_gaps}; Selected gap groups: {len(groups)}; "
              f"LLM call budget: {budget.maximum}", flush=True)
        try:
            with use_call_budget(budget):
                claims = [item for task in plan.tasks for item in store.get_claims_for_task(task.id)]
                rr_plan = await asyncio.to_thread(self.reresearch_planner.create_plan, query, plan, groups, claims)
                rr_plan = ReResearchPlan.model_validate(rr_plan.model_dump())
                # Treat even injected/custom planners as untrusted at the scheduling boundary.
                expected = {group.task_id: set(group.gap_ids) for group in groups}
                if (len(rr_plan.tasks) != len(groups) or rr_plan.round_id != 1
                        or {task.origin_task_id: set(task.origin_gap_ids) for task in rr_plan.tasks} != expected
                        or any(task.id in {item.id for item in plan.tasks} or task.status != "pending"
                               for task in rr_plan.tasks)):
                    raise ValueError("Supplemental plan does not match the selected groups or round budget.")
                summary.plans.append(rr_plan)
                summary.rounds = 1
                for task in rr_plan.tasks:
                    group = next(group for group in groups if group.task_id == task.origin_task_id)
                    print(f"[{task.id}] origin_task={task.origin_task_id} "
                          f"gap_types={','.join(kind.value for kind in group.gap_types)}", flush=True)
                    if settings.show_gap_debug:
                        print(f"  origin_gap_ids={task.origin_gap_ids}", flush=True)
                new_claims = []
                # Same bounded gather/to_thread mechanism as the original scheduler.
                for offset in range(0, len(rr_plan.tasks), self.max_concurrency):
                    if budget.exhausted:
                        raise ReResearchBudgetExhausted("Re-Research budget exhausted.")
                    batch = rr_plan.tasks[offset:offset + self.max_concurrency]
                    summary.tasks += len(batch)
                    print(f"正在执行补充研究：{len(batch)} 项（并发上限 {self.max_concurrency}）", flush=True)
                    outcomes = await asyncio.gather(
                        *(self._run_task(task, {}, query, store, []) for task in batch), return_exceptions=True)
                    for task, outcome in zip(batch, outcomes):
                        if isinstance(outcome, BaseException):
                            summary.failures[task.id] = f"{type(outcome).__name__}: {outcome}"
                            continue
                        try:
                            bundle = outcome.evidence_bundle
                            if bundle is None or bundle.task_id != task.id:
                                raise ValueError("Supplemental task returned no matching evidence bundle.")
                            old_ids = {item.claim_id for item in store.get_claims_for_task(task.id)}
                            counts = store.counts
                            canonical = store.merge(bundle)
                            supplemental[task.id] = outcome.model_copy(update={"evidence_bundle": canonical}, deep=True)
                            added = [item for item in canonical.claims if item.claim_id not in old_ids]
                            new_claims.extend(added)
                            if not added:
                                summary.failures[task.id] = "Supplemental task produced no new formal claims."
                            after = store.counts
                            print(f"[{task.id}] evidence: new_sources={after['sources'] - counts['sources']} "
                                  f"new_evidence={after['evidence'] - counts['evidence']} new_claims={len(added)}", flush=True)
                        except Exception as error:
                            task.status = "failed"
                            summary.failures[task.id] = f"{type(error).__name__}: {error}"
                for task in rr_plan.tasks:
                    batch_claims = [item for item in new_claims if item.task_id == task.id]
                    if not batch_claims:
                        continue
                    try:
                        decisions = await asyncio.to_thread(self.verifier.verify_batch, batch_claims, store)
                        expected_claims = {item.claim_id: item for item in batch_claims}
                        if (len(decisions) != len(batch_claims)
                                or {item.claim_id for item in decisions} != set(expected_claims)
                                or any(item.task_id != task.id or set(item.evidence_ids)
                                       != set(expected_claims[item.claim_id].evidence_ids) for item in decisions)):
                            raise ValueError("Supplemental verification does not match the new claim support chains.")
                        for item in decisions:
                            verified.add_verification(item)
                    except ReResearchBudgetExhausted:
                        raise
                    except Exception as error:
                        summary.failures[task.id] = f"verification: {type(error).__name__}: {error}"
                    finally:
                        usage = self.verifier.last_usage
                        verification_usage.add(usage)
                        summary.verification_calls += usage.llm_calls
                # Assess only new claims whose verification succeeded; one compact batch pass.
                eligible = [item for item in new_claims if verified.get_latest_verification(item.claim_id) is not None]
                if eligible:
                    assessor = self.gap_detector.assessor
                    try:
                        decisions = await asyncio.to_thread(assessor.assess_batch, query, rr_plan.tasks, eligible)
                        if (len(decisions) != len(eligible)
                                or {item.claim_id for item in decisions} != {item.claim_id for item in eligible}
                                or any(item.task_id != next(claim.task_id for claim in eligible if claim.claim_id == item.claim_id)
                                       for item in decisions)):
                            raise ValueError("Supplemental relevance returned an incomplete claim decision set.")
                        relevance_results.update((item.claim_id, item) for item in decisions)
                    finally:
                        usage = assessor.last_usage
                        relevance_usage.add(usage)
                        summary.relevance_calls += usage.llm_calls
                latest = {item.claim_id: item for item in verified.latest_results}
                for task in rr_plan.tasks:
                    status = update_group_status(gaps, task.origin_gap_ids,
                        [item for item in new_claims if item.task_id == task.id], latest, relevance_results)
                    print(f"[{task.id}] gap result: {status.value} gaps={len(task.origin_gap_ids)}", flush=True)
                if summary.failures:
                    summary.stop_reason = "round limit reached; supplemental task failures"
                if budget.exhausted:
                    summary.stop_reason = "budget exhausted"
        except ReResearchBudgetExhausted:
            summary.stop_reason = "budget exhausted"
        except Exception as error:
            summary.stop_reason = "supplemental stage failed"
            summary.failures["stage"] = f"{type(error).__name__}: {error}"
        finally:
            budget.close()
            summary.llm_calls = budget.calls
            summary.calls_by_stage = dict(budget.by_stage)
            after = store.counts
            summary.new_sources = after['sources'] - before['sources']
            summary.new_evidence = after['evidence'] - before['evidence']
            summary.new_claims = after['claims'] - before['claims']
            for task_id, reason in summary.failures.items():
                print(f"[Re-Research] {task_id} failed: {reason}", flush=True)
            print(f"[Re-Research] STOP: {summary.stop_reason}; "
                  f"additional_llm_calls={summary.llm_calls}/{budget.maximum}", flush=True)
        return supplemental, summary

    @staticmethod
    def _print_reresearch(summary, gaps):
        counts = Counter(item.status.value for item in gaps.all_gaps())
        print(f"\nGap Summary Before Re-Research: open={summary.initial_gaps}", flush=True)
        print(f"Re-Research Summary: rounds={summary.rounds} selected_groups={summary.selected_groups} "
              f"tasks={summary.tasks} new_sources={summary.new_sources} new_evidence={summary.new_evidence} "
              f"new_claims={summary.new_claims} verification_calls={summary.verification_calls} "
              f"relevance_calls={summary.relevance_calls} additional_llm_calls={summary.llm_calls} "
              f"stop_reason={summary.stop_reason}", flush=True)
        print(f"Gap Summary After Re-Research: resolved={counts['RESOLVED']} improved={counts['IMPROVED']} "
              f"open={counts['OPEN']}", flush=True)
        print("Re-Research call usage: " + " ".join(
            f"{stage}={summary.calls_by_stage.get(stage, 0)}"
            for stage in ("planner", "researcher", "extraction", "verification", "relevance")), flush=True)

    @staticmethod
    def _print_gaps(results: dict[str, ClaimRelevanceResult], usage: RelevanceUsage, store: GapStore) -> None:
        counts = Counter(item.status.value for item in results.values())
        print(f"\nRelevance Summary: claims={len(results)} aligned={counts['ALIGNED']} "
              f"partial={counts['PARTIAL']} misaligned={counts['MISALIGNED']} llm_calls={usage.llm_calls} "
              f"relevance_context_chars={usage.context_chars}", flush=True)
        if usage.usage_responses:
            print(f"Relevance token usage: input_tokens={usage.input_tokens} output_tokens={usage.output_tokens} "
                  f"usage_responses={usage.usage_responses}/{usage.llm_calls}", flush=True)
        gaps = store.all_gaps()
        kinds = Counter(item.gap_type.value for item in gaps)
        print(f"\nGap Summary: support={kinds['SUPPORT_GAP']} relevance={kinds['RELEVANCE_GAP']} "
              f"coverage={kinds['COVERAGE_GAP']} total={len(gaps)}", flush=True)
        if get_settings().show_gap_debug:
            for item in gaps:
                print(f"[{item.task_id}] gap_id={item.gap_id} claim_id={item.claim_id} gap_type={item.gap_type.value}\n"
                      f"  status: {item.status.value}\n  description: {item.description}\n  missing_information: {item.missing_information}", flush=True)

    @staticmethod
    def _print_verification(store: VerificationStore, usage: VerificationUsage) -> None:
        latest = store.latest_results
        counts = Counter(item.status.value for item in latest)
        print(f"\nVerification Summary: claims={len(latest)} supported={counts['SUPPORTED']} "
              f"partial={counts['PARTIAL']} unsupported={counts['UNSUPPORTED']} "
              f"llm_calls={usage.llm_calls} verification_context_chars={usage.context_chars}", flush=True)
        if usage.usage_responses:
            print(f"Verification token usage: input_tokens={usage.input_tokens} output_tokens={usage.output_tokens} "
                  f"usage_responses={usage.usage_responses}/{usage.llm_calls}", flush=True)
        if get_settings().show_verification_debug:
            for item in latest:
                print(f"[{item.task_id}] verification {item.claim_id}: {item.status.value} — {item.reason}", flush=True)

    @staticmethod
    def _get_ready_tasks(
        plan: ResearchPlan, executions: dict[str, ResearchExecutionResult],
    ) -> list[ResearchTask]:
        tasks_by_id = {task.id: task for task in plan.tasks}
        return [
            task for task in plan.tasks
            if task.status == "pending" and all(
                tasks_by_id[dependency].status == "completed" and dependency in executions
                for dependency in task.depends_on
            )
        ]

    @staticmethod
    def _evidence_dependencies(task: ResearchTask, plan: ResearchPlan) -> list[str]:
        dependencies = list(task.depends_on)
        if task.task_type == "synthesis":
            tasks = {item.id: item for item in plan.tasks}
            for key in dependencies:
                for parent in tasks[key].depends_on:
                    if parent not in dependencies:
                        dependencies.append(parent)
        return dependencies

    async def _run_task(
        self, task: ResearchTask, executions: dict[str, ResearchExecutionResult], query: str,
        evidence_store: EvidenceStore,
        evidence_dependencies: list[str] | None = None,
    ) -> ResearchExecutionResult:
        task.status = "running"
        try:
            # Isolate mutable compact results even when sibling tasks share dependencies.
            dependency_context = {
                dependency: executions[dependency].compact.model_copy(deep=True)
                for dependency in task.depends_on
            }
            if isinstance(self.researcher, Researcher):
                allowed = task.depends_on if evidence_dependencies is None else evidence_dependencies
                artifacts = build_evidence_context(task, evidence_store, allowed)
                if task.depends_on:
                    print(f"[{task.id}] upstream evidence: claims={len(artifacts.claims)} "
                          f"evidence={len(artifacts.evidence)} sources={len(artifacts.sources)}", flush=True)
                    available = sum(len(evidence_store.get_claims_for_task(key)) for key in allowed)
                    represented = {claim.task_id for claim in artifacts.claims}
                    omitted = [key for key in allowed if key not in represented]
                    print(f"[{task.id}] upstream coverage: tasks={len(represented)}/{len(allowed)} "
                          f"claims={len(artifacts.claims)}/{available} omitted_tasks={omitted}", flush=True)
                _, context_text = self.researcher.build_request(task, dependency_context, query, artifacts)
                if artifacts.evidence:
                    context_text += "\n" + artifacts.to_json()
            else:
                context_text = build_dependency_context(dependency_context)
            print(
                f"[{task.id}] 正在执行\n"
                f"[{task.id}] 任务类型：{task.task_type}\n"
                f"[{task.id}] 依赖：{', '.join(task.depends_on) or '无'}\n"
                f"[{task.id}] 依赖上下文字符数：{len(context_text)}",
                flush=True,
            )
            arguments = {"task": task, "context": dependency_context, "goal": query}
            if isinstance(self.researcher, Researcher):
                arguments["evidence_context"] = artifacts
            execution = await asyncio.to_thread(self.researcher.research, **arguments)
        except Exception as error:
            task.status = "failed"
            print(f"[{task.id}] failed — {type(error).__name__}: {error}", flush=True)
            raise
        task.status = "completed"
        print(f"[{task.id}] completed", flush=True)
        return execution

    @staticmethod
    def _print_evidence(plan: ResearchPlan, store: EvidenceStore, supplemental_tasks=()) -> None:
        counts = store.counts
        reuse = store.reuse_counts
        print(f"\nEvidence Store: sources={counts['sources']} evidence={counts['evidence']} claims={counts['claims']} "
              f"source_reuse={reuse['source_reuse']} evidence_reuse={reuse['evidence_reuse']}", flush=True)
        if get_settings().show_evidence_debug:
            for task in [*plan.tasks, *supplemental_tasks]:
                for claim in store.get_claims_for_task(task.id):
                    print(f"[{task.id}] Claim: {claim.text}", flush=True)
                    print(f"  claim_id: {claim.claim_id}\n  evidence_ids: {claim.evidence_ids}", flush=True)
                    for item in store.get_evidence_for_claim(claim.claim_id):
                        source = store.get_source_for_evidence(item.evidence_id)
                        print(f"  Evidence: {item.content}\n  Source: {source.title} — {source.url}", flush=True)
                        print(f"  evidence_id: {item.evidence_id} → source_id: {source.source_id}", flush=True)
                        print(f"  Evidence provenance: {store.get_evidence_provenance(item.evidence_id)}\n"
                              f"  Source provenance: {store.get_source_provenance(source.source_id)}", flush=True)

    @staticmethod
    def _print_results(plan: ResearchPlan, executions: dict[str, ResearchExecutionResult]) -> None:
        """Print successful memos in the original plan order, regardless of finish order."""
        for task in plan.tasks:
            if task.id not in executions:
                continue
            execution = executions[task.id]
            counts = Counter(execution.tools_used)
            tools_line = ", ".join(
                f"{name} x{counts[name]}" for name in ("web_search", "fetch_webpage") if counts[name]
            )
            print(
                "\n" + "=" * 40 + f"\n研究摘要 — {task.id}\n" + "=" * 40
                + f"\n任务类型：{task.task_type}"
                + f"\n依赖：{', '.join(task.depends_on) or '无'}"
                + f"\n工具调用：{tools_line or '无'}"
                + f"\n压缩结果字符数：{len(execution.compact.model_dump_json())}"
                + "\n" + execution.memo,
                flush=True,
            )
