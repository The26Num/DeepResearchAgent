"""Strict JSON decoding and graph/stage validation before any resumed model call."""

import json

from pydantic import ValidationError

from app.persistence.models import ResearchRunState, STATE_VERSION
from app.research.evidence_store import EvidenceStore
from app.research.verification_store import VerificationStore
from app.research.gap_store import GapStore
from app.schemas.plan import validate_plan_dependencies


class StateIntegrityError(ValueError):
    pass


class IncompatibleCheckpointError(ValueError):
    pass


def validate_restored_state(state: ResearchRunState) -> None:
    if state.state_version != STATE_VERSION:
        raise IncompatibleCheckpointError(f"Unsupported state_version={state.state_version}; expected {STATE_VERSION}.")
    try:
        allowed_options = {"verify_claims", "detect_gaps", "reresearch", "write_report"}
        options = state.phase_options
        if (set(options) - allowed_options or (options.get("detect_gaps", True) and not options.get("verify_claims", True))
                or (options.get("reresearch", True) and not options.get("detect_gaps", True))
                or (options.get("write_report", True) and not options.get("detect_gaps", True))):
            raise ValueError("Inconsistent phase options in checkpoint.")
        store = EvidenceStore.restore(state.evidence_store.model_dump(mode="json"))
        verified = VerificationStore.restore([item.model_dump(mode="json") for item in state.verification_results])
        if state.research_plan is None:
            if (state.completed_stages - {"RUN_CREATED"} or state.task_results or any(store.counts.values())
                    or state.verification_results or state.relevance_results or state.gaps
                    or state.supplemental_results or state.reresearch_summary.plans or state.research_report
                    or state.writing_context or state.rendered_report):
                raise ValueError("Checkpoint has research state without a ResearchPlan.")
            return  # RUN_CREATED permits recovery even if planning was interrupted.
        validate_plan_dependencies(state.research_plan)
        initial_tasks = {item.id: item for item in state.research_plan.tasks}
        tasks = dict(initial_tasks)
        for plan in state.reresearch_summary.plans:
            for task in plan.tasks:
                if task.id in tasks or task.origin_task_id not in initial_tasks:
                    raise ValueError("Invalid supplemental task identity/origin.")
                tasks[task.id] = task
        if len(state.reresearch_summary.plans) > 1:
            raise ValueError("Phase 6 supports the existing single supplemental round only.")
        executions = {**state.task_results, **state.supplemental_results}
        if set(state.task_results) - set(initial_tasks) or set(state.supplemental_results) - (set(tasks) - set(initial_tasks)):
            raise ValueError("Unknown task result in checkpoint.")
        claims = {item.claim_id: item for item in store.all_claims()}
        for item in [*state.evidence_store.sources, *state.evidence_store.evidence, *claims.values()]:
            if item.task_id not in tasks:
                raise ValueError("Artifact references an unknown task.")
        for users in [*state.evidence_store.source_users.values(), *state.evidence_store.evidence_users.values()]:
            if set(users) - set(tasks):
                raise ValueError("Provenance references an unknown task.")
        if any(origin[0] not in tasks for origin in state.evidence_store.evidence_alias_origins.values()):
            raise ValueError("Evidence alias references an unknown origin task.")
        for result in state.verification_results:
            claim = claims.get(result.claim_id)
            if claim is None or result.task_id != claim.task_id or set(result.evidence_ids) != set(claim.evidence_ids):
                raise ValueError("VerificationResult references a missing/mismatched Claim.")
        for key, result in state.relevance_results.items():
            claim = claims.get(key)
            if claim is None or key != result.claim_id or result.task_id != claim.task_id:
                raise ValueError("Relevance result references a missing/mismatched Claim.")
        for key, result in executions.items():
            if result.compact.task_id != key:
                raise ValueError("Task result has a mismatched compact task ID.")
            if result.evidence_bundle:
                if result.evidence_bundle.task_id != key:
                    raise ValueError("Task result bundle has a mismatched task ID.")
                for claim in result.evidence_bundle.claims:
                    if claims.get(claim.claim_id) != claim:
                        raise ValueError("Task result does not match the canonical Claim graph.")
        for task in tasks.values():
            if task.status == "completed":
                if task.id not in executions:
                    raise ValueError(f"Completed task {task.id} has no result.")
                if state.phase_options.get("verify_claims", True):
                    if any(verified.get_latest_verification(item.claim_id) is None
                           for item in store.get_claims_for_task(task.id)):
                        raise ValueError(f"Completed task {task.id} has unfinished Verification.")
            if task.id in initial_tasks and task.status == "completed":
                if any(initial_tasks[key].status != "completed" for key in task.depends_on):
                    raise ValueError("Completed task has an unfinished dependency.")
        gap_store = GapStore()
        for gap in state.gaps:
            if gap.task_id not in tasks or (gap.claim_id is not None and
                    (gap.claim_id not in claims or claims[gap.claim_id].task_id != gap.task_id)):
                raise ValueError("Gap references a missing/mismatched task or Claim.")
            if any(item.gap_id == gap.gap_id for item in gap_store.all_gaps()):
                raise ValueError("Duplicate Gap ID.")
            if gap_store.add_gap(gap).gap_id != gap.gap_id:
                raise ValueError("Duplicate Gap identity.")
        gap_ids = {item.gap_id for item in state.gaps}
        for plan in state.reresearch_summary.plans:
            if set(plan.selected_gap_ids) - gap_ids:
                raise ValueError("Supplemental plan references an unknown Gap.")
        for group in state.selected_gap_groups:
            if group.task_id not in initial_tasks or set(group.gap_ids) - gap_ids:
                raise ValueError("Selected supplemental group references missing state.")
        stages = state.completed_stages
        for stage in stages:
            if stage.startswith("TASK_COMPLETED:"):
                task = initial_tasks.get(stage.split(":", 1)[1])
                if task is None or task.status != "completed":
                    raise ValueError("Completed task marker does not match its task status.")
        if "INITIAL_RESEARCH_COMPLETED" in stages and any(item.status != "completed" for item in initial_tasks.values()):
            raise ValueError("Initial research stage is incomplete.")
        if "GAP_DETECTION_COMPLETED" in stages:
            if "INITIAL_RESEARCH_COMPLETED" not in stages:
                raise ValueError("Gap stage precedes initial research.")
            for claim in claims.values():
                if claim.task_id in initial_tasks and (verified.get_latest_verification(claim.claim_id) is None
                                                      or claim.claim_id not in state.relevance_results):
                    raise ValueError("Completed Gap Detection has unfinished decisions.")
        if "RERESEARCH_COMPLETED" in stages and "GAP_DETECTION_COMPLETED" not in stages:
            raise ValueError("Supplemental stage precedes Gap Detection.")
        if "WRITER_COMPLETED" in stages:
            if state.research_report is None or state.writing_context is None or "GAP_DETECTION_COMPLETED" not in stages:
                raise ValueError("Completed Writer has no grounded report/context.")
            if options.get("reresearch", True) and "RERESEARCH_COMPLETED" not in stages:
                raise ValueError("Writer precedes the supplemental stage.")
            from app.agents.writer import validate_report
            if state.writing_context.accepted_claims:
                validate_report(state.research_report, state.writing_context,
                    allow_insufficient=state.writer_usage.fallback_mode == "insufficient_material")
            elif state.research_report.factual_paragraphs():
                raise ValueError("Zero accepted Claim report contains factual paragraphs.")
            for key in state.research_report.used_claim_ids:
                if (key not in claims or verified.get_latest_verification(key) is None
                        or verified.get_latest_verification(key).status != "SUPPORTED"
                        or key not in state.relevance_results or state.relevance_results[key].status != "ALIGNED"):
                    raise ValueError("Report uses a non-accepted Claim.")
        if "CITATION_COMPLETED" in stages:
            if "WRITER_COMPLETED" not in stages or state.rendered_report is None:
                raise ValueError("Completed Citation has no completed Writer/rendered report.")
            from app.research.report_rendering import validate_citations
            validate_citations(state.research_report, state.rendered_report, store, debug=state.citation_debug)
        if "RUN_COMPLETED" in stages:
            expected = "CITATION_COMPLETED" if state.phase_options.get("write_report", True) else "INITIAL_RESEARCH_COMPLETED"
            if expected not in stages:
                raise ValueError("Run completion is missing its final stage.")
            for option, stage in (("detect_gaps", "GAP_DETECTION_COMPLETED"), ("reresearch", "RERESEARCH_COMPLETED")):
                if options.get(option, True) and stage not in stages:
                    raise ValueError("Run completion is missing an enabled stage.")
        if state.reresearch_budget:
            from app.research.reresearch_budget import ReResearchCallBudget
            ReResearchCallBudget.restore(state.reresearch_budget)
    except (ValueError, KeyError, TypeError) as error:
        raise StateIntegrityError(f"Invalid checkpoint: {error}") from error


def state_from_json(text: str) -> ResearchRunState:
    try:
        raw = json.loads(text)
        if raw.get("state_version") != STATE_VERSION:
            raise IncompatibleCheckpointError(f"Unsupported state_version={raw.get('state_version')}; expected {STATE_VERSION}.")
        state = ResearchRunState.model_validate(raw)
    except (ValidationError, json.JSONDecodeError, AttributeError) as error:
        raise StateIntegrityError(f"Malformed checkpoint JSON: {type(error).__name__}.") from error
    validate_restored_state(state)
    return state
