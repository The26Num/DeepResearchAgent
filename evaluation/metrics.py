"""Deterministic diagnostics. No model, tool, or research-state mutation."""

from collections import Counter
from datetime import datetime
import re
from statistics import median

from app.persistence.models import ResearchRunState
from app.research.evidence_store import EvidenceStore
from app.research.report_rendering import validate_citations
from app.schemas.source import normalize_source_url
from evaluation.models import BenchmarkCase, MetricVector


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def normalize_paragraph(text):
    return " ".join(text.split()).casefold()


def duplication(texts):
    values = [normalize_paragraph(item) for item in texts if item.strip()]
    duplicates = len(values) - len(set(values))
    return duplicates, ratio(duplicates, len(values))


def efficiency_metrics(telemetry=None, *, state=None):
    """Complete tokens require every call to return usage; otherwise retain a subtotal."""
    if telemetry is None:
        stages = {}
        if state is not None:
            for name, usage in (("verification", state.verification_usage), ("relevance", state.relevance_usage),
                                ("writer", state.writer_usage), ("grounding", state.writer_usage.grounding)):
                stages[name] = {"llm_calls": usage.llm_calls, "model_responses": None,
                    "usage_responses": usage.usage_responses, "observed_input_tokens": usage.input_tokens,
                    "observed_output_tokens": usage.output_tokens}
        tools_used = [tool for result in ([*state.task_results.values(), *state.supplemental_results.values()] if state else [])
                      for tool in result.tools_used]
        tools = {"web_search_calls": tools_used.count("web_search") if state else None,
                 "fetch_webpage_calls": tools_used.count("fetch_webpage") if state else None,
                 "successful_fetches": None, "failed_fetches": None, "pdf_failure_count": None}
        calls_complete = False  # Phase 6 did not persist Researcher/Supervisor tokens/calls.
    else:
        stages, tools = telemetry["stages"], telemetry["tools"]
        calls_complete = True
    stages = {key: dict(value) for key, value in stages.items()}
    for value in stages.values():
        value["usage_coverage"] = ratio(value["usage_responses"], value["model_responses"])
        complete = (value["model_responses"] == value["llm_calls"] == value["usage_responses"])
        value["input_tokens"] = value["observed_input_tokens"] if complete else None
        value["output_tokens"] = value["observed_output_tokens"] if complete else None
        value["total_tokens"] = value["input_tokens"] + value["output_tokens"] if complete else None
    calls = sum(item["llm_calls"] for item in stages.values())
    responses = sum(item["model_responses"] or 0 for item in stages.values()) if calls_complete else None
    usages = sum(item["usage_responses"] for item in stages.values())
    observed_in = sum(item["observed_input_tokens"] for item in stages.values())
    observed_out = sum(item["observed_output_tokens"] for item in stages.values())
    complete = calls_complete and calls == responses == usages
    result = {**tools, "llm_count_scope": "LangChain model invocation starts, including failed or guard-rejected attempts",
              "fetch_success_rate": ratio(tools["successful_fetches"], tools["fetch_webpage_calls"])
              if tools["successful_fetches"] is not None else None,
              "calls_by_stage": stages, "total_llm_calls": calls if calls_complete else None,
              "observed_llm_calls": calls, "llm_calls_complete": calls_complete,
              "model_responses": responses, "usage_responses": usages, "usage_coverage": ratio(usages, responses),
              "model_no_response_count": calls - responses if responses is not None else None,
              "model_response_rate": ratio(responses, calls) if responses is not None else None,
              "tokens_complete": complete, "input_tokens": observed_in if complete else None,
              "output_tokens": observed_out if complete else None,
              "total_tokens": observed_in + observed_out if complete else None,
              "observed_input_tokens": observed_in if usages else None,
              "observed_output_tokens": observed_out if usages else None,
              "observed_total_tokens": observed_in + observed_out if usages else None,
              "citation_calls": 0 if state is not None else None}
    for name in ("supervisor", "researcher", "extraction", "verification", "relevance", "reresearch_planner", "writer", "grounding"):
        result[name + "_calls"] = stages.get(name, {}).get("llm_calls", 0 if calls_complete else None)
    return result


def baseline_citation_metrics(report, sources):
    provided = {item.source_id for item in sources}
    markers = re.findall(r"\[(S[A-Za-z0-9_-]+)\]", report)
    unknown = sorted(set(markers) - provided)
    # This checks source-ID validity only; it cannot prove sentence support.
    return {"citation_integrity_pass": not unknown, "citation_error_count": len(unknown),
            "unknown_citations": unknown, "cited_sources": len(set(markers) & provided),
            "citation_check_scope": "provided source IDs only"}


class Evaluator:
    def evaluate(self, state: ResearchRunState, case: BenchmarkCase, *, run=None, checkpoint_count=None,
                 telemetry=None, runtime_seconds=None, before_gaps=None) -> MetricVector:
        if state.goal.strip() != case.question.strip():
            raise ValueError("Benchmark question does not match the Research Run goal.")
        snapshot = state.evidence_store
        claims = {item.claim_id: item for item in snapshot.claims}
        evidence = {item.evidence_id: item for item in snapshot.evidence}
        sources = {item.source_id: item for item in snapshot.sources}
        errors = []
        verified = {item.claim_id: item for item in state.verification_results}
        relevant = dict(state.relevance_results)
        for decisions in (verified, relevant):
            for key, item in list(decisions.items()):
                if key not in claims or item.claim_id != key or item.task_id != claims[key].task_id:
                    errors.append(f"Unknown or mismatched decision: {key}")
                    decisions.pop(key)
        for key, item in list(verified.items()):
            if set(item.evidence_ids) != set(claims[key].evidence_ids):
                errors.append(f"Stale verification evidence: {key}")
                verified.pop(key)
        accepted = {key for key in claims if key in verified and key in relevant
                    and verified[key].status == "SUPPORTED" and relevant[key].status == "ALIGNED"}
        assessed = set(verified) | set(relevant)
        both_assessed = set(verified) & set(relevant)
        v = Counter(item.status.value for item in verified.values())
        r = Counter(item.status.value for item in relevant.values())
        link_count, used_evidence, evidence_sources = 0, set(), {}
        with_evidence = 0
        for claim in claims.values():
            with_evidence += bool(claim.evidence_ids)
            if not claim.evidence_ids:
                errors.append(f"Claim without Evidence: {claim.claim_id}")
            for key in claim.evidence_ids:
                link_count += 1
                used_evidence.add(key)
                fragment = evidence.get(key)
                if fragment is None or fragment.source_id not in sources:
                    errors.append(f"Dangling Evidence/Source: {claim.claim_id} -> {key}")
                else:
                    evidence_sources[key] = fragment.source_id
        for item in evidence.values():
            if item.source_id not in sources:
                errors.append(f"Unknown Source for Evidence: {item.evidence_id}")
        accepted_sources = {evidence_sources[key] for cid in accepted for key in claims[cid].evidence_ids if key in evidence_sources}
        citation_errors = []
        citation_pass = None
        report, rendered = state.research_report, state.rendered_report
        if report is not None and rendered is not None:
            try:
                store = EvidenceStore.restore(snapshot.model_dump(mode="json"))
                validate_citations(report, rendered, store, debug=state.citation_debug)
                citation_pass = True
            except (ValueError, KeyError) as error:
                citation_errors.append(str(error))
                citation_pass = False
        elif "RUN_COMPLETED" in state.completed_stages and state.phase_options.get("write_report", True):
            citation_errors.append("Completed report or rendered citations are missing.")
            citation_pass = False
        cited_sources = {item.source_id for item in rendered.references} if rendered else set()
        used_claims = set(report.used_claim_ids) if report else set()
        if used_claims - accepted:
            errors.append("Report references non-publishable Claims.")
        original_tasks = state.research_plan.tasks if state.research_plan else []
        covered = {claims[key].task_id for key in accepted}
        original_covered = {item.id for item in original_tasks} & covered
        by_type = {}
        for kind in ("discovery", "analysis", "synthesis"):
            ids = {item.id for item in original_tasks if item.task_type == kind}
            by_type[kind] = {"tasks": len(ids), "covered": len(ids & covered), "rate": ratio(len(ids & covered), len(ids))}
        origins = {task.id: task.origin_task_id for plan in state.reresearch_summary.plans for task in plan.tasks}
        supplemented = original_covered | ({origins[key] for key in covered if key in origins} & {t.id for t in original_tasks})
        selected = {key for group in state.selected_gap_groups for key in group.gap_ids}
        if not selected:
            selected = {key for plan in state.reresearch_summary.plans for key in plan.selected_gap_ids}
        selected_gaps = [item for item in state.gaps if item.gap_id in selected]
        resolved = sum(item.status == "RESOLVED" for item in selected_gaps)
        improved = sum(item.status == "IMPROVED" for item in selected_gaps)
        supplemental_ids = set(state.supplemental_results) | set(origins)
        new_claims = {key for key, item in claims.items() if item.task_id in supplemental_ids}
        # For partial/failure snapshots, actual merged claims are authoritative.
        new_count = len(new_claims)
        summary = state.reresearch_summary
        before_count = len(before_gaps) if before_gaps is not None else len(state.gaps)
        after_open = sum(item.status != "RESOLVED" for item in state.gaps)
        gaps_by_type = Counter(item.gap_type.value for item in state.gaps)
        markdown = rendered.markdown if rendered else ""
        paragraphs = ([item.text for item in report.factual_paragraphs()] + [item.text for item in report.limitations]) if report else []
        duplicates, duplicate_rate = duplication(paragraphs)
        limitation_duplicates, _ = duplication([item.text for item in report.limitations] if report else [])
        grounding = state.writer_usage.grounding
        years = [int(match.group()) for item in sources.values() if item.published_at
                 for match in [re.search(r"\b(?:19|20)\d{2}\b", item.published_at)] if match]
        gold = {normalize_source_url(url) for url in case.gold_sources or []}
        stored_urls = {normalize_source_url(item.url) for item in sources.values()}
        research = {"total_claims": len(claims), "verified_claims": len(verified), "relevance_assessed_claims": len(relevant),
            "assessed_claims": len(assessed), "both_assessed_claims": len(both_assessed), "publishable_claims": len(accepted),
            "unverified_claims": len(set(claims) - set(verified)), "unassessed_relevance_claims": len(set(claims) - set(relevant)),
            "supported_claims": v["SUPPORTED"], "partial_claims": v["PARTIAL"], "unsupported_claims": v["UNSUPPORTED"],
            "aligned_claims": r["ALIGNED"], "partial_alignment_claims": r["PARTIAL"], "misaligned_claims": r["MISALIGNED"],
            "claims_with_evidence": with_evidence, "unique_evidence": len(evidence), "unique_linked_evidence": len(used_evidence),
            "total_claim_evidence_links": link_count, "stored_sources": len(sources), "accepted_sources": len(accepted_sources),
            "cited_sources": len(cited_sources), "source_type_distribution": dict(Counter(item.source_type for item in sources.values())),
            "source_year_distribution": dict(Counter(str(year) for year in years)), "median_source_year": median(years) if years else None,
            "source_dates_available": len(years), "original_tasks": len(original_tasks), "covered_tasks": len(original_covered),
            "task_coverage_by_type": by_type, "support_gaps": gaps_by_type["SUPPORT_GAP"],
            "relevance_gaps": gaps_by_type["RELEVANCE_GAP"], "coverage_gaps": gaps_by_type["COVERAGE_GAP"],
            "before_reresearch_open_gaps": before_count, "after_reresearch_open_gaps": after_open,
            "after_reresearch_strict_open_gaps": sum(item.status == "OPEN" for item in state.gaps),
            "resolved_gaps": sum(item.status == "RESOLVED" for item in state.gaps),
            "improved_gaps": sum(item.status == "IMPROVED" for item in state.gaps), "selected_gap_count": len(selected),
            "selected_resolved_gaps": resolved, "selected_improved_gaps": improved,
            "reresearch_new_sources": summary.new_sources, "reresearch_new_evidence": summary.new_evidence,
            "reresearch_new_claims": new_count, "reresearch_new_publishable_claims": len(new_claims & accepted),
            "reresearch_stop_reason": summary.stop_reason}
        quality = {"support_rate": ratio(v["SUPPORTED"], len(verified)), "partial_rate": ratio(v["PARTIAL"], len(verified)),
            "unsupported_rate": ratio(v["UNSUPPORTED"], len(verified)), "alignment_rate": ratio(r["ALIGNED"], len(relevant)),
            "partial_alignment_rate": ratio(r["PARTIAL"], len(relevant)), "misaligned_rate": ratio(r["MISALIGNED"], len(relevant)),
            "publishable_claim_rate": ratio(len(accepted), len(assessed)), "publishable_all_claim_rate": ratio(len(accepted), len(claims)),
            "assessment_completion_rate": ratio(len(both_assessed), len(claims)),
            "evidence_coverage": ratio(with_evidence, len(claims)),
            "evidence_reuse_ratio": 1 - len(used_evidence) / link_count if link_count else None,
            "source_utilization_rate": ratio(len(cited_sources), len(sources)),
            "accepted_source_utilization_rate": ratio(len(accepted_sources), len(sources)),
            "source_recall": ratio(len(gold & stored_urls), len(gold)),
            "task_coverage_rate": ratio(len(original_covered), len(original_tasks)),
            "task_coverage_including_reresearch_rate": ratio(len(supplemented), len(original_tasks)),
            "writer_claim_coverage": ratio(len(used_claims & accepted), len(accepted)),
            "citation_integrity_pass": citation_pass, "citation_error_count": len(citation_errors), "citation_errors": citation_errors,
            "grounding_checked_items": grounding.checked_items, "grounding_repaired_items": grounding.repaired_items,
            "grounding_repair_rate": ratio(grounding.repaired_items, grounding.checked_items),
            "grounding_fallback_reason": grounding.fallback_reason or None,
            "grounding_failed_items": sum(item.status != "SUPPORTED" for item in state.report_grounding),
            "gap_resolved_rate": ratio(resolved, len(selected)), "gap_improved_rate": ratio(improved, len(selected)),
            "gap_repair_rate": ratio(resolved + improved, len(selected)),
            "reresearch_publishable_yield": ratio(len(new_claims & accepted), new_count),
            "duplicate_paragraph_count": duplicates, "duplicate_paragraph_rate": duplicate_rate,
            "duplicate_limitation_count": limitation_duplicates, "report_chars": len(markdown),
            "report_words": len(re.findall(r"\b\w+\b", markdown)),
            "structural_integrity_pass": not errors and citation_pass is not False,
            "structural_errors": errors}
        timing, reliability = {"total_runtime_seconds": runtime_seconds}, {}
        if run is not None:
            elapsed = (datetime.fromisoformat(run.updated_at) - datetime.fromisoformat(run.created_at)).total_seconds()
            timing["run_elapsed_including_resume_wait_seconds"] = elapsed
            if runtime_seconds is None:
                timing["total_runtime_seconds"] = elapsed
            reliability = {"resume_count": run.resume_count, "checkpoint_count": checkpoint_count,
                "run_status": run.status.value, "was_resumed": run.resume_count > 0,
                "failed_at_evaluation": run.status == "FAILED", "interrupted_at_evaluation": run.status == "INTERRUPTED",
                "historical_failure": None, "historical_interruption": None}
        return MetricVector(research=research, quality=quality, efficiency=efficiency_metrics(telemetry, state=state),
                            timing=timing, reliability=reliability)
