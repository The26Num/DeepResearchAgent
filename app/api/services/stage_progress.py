"""Public phase presentation derived from existing checkpoints, not decisions."""

from app.research.events import PhaseProgress

PHASES = ("planning", "research", "verification", "gap_detection", "reresearch", "writer", "citation")


def phase_for_stage(stage):
    if stage.startswith("RERESEARCH"):
        return "reresearch"
    if stage.startswith("TASK_VERIFICATION"):
        return "verification"
    if stage.startswith(("INITIAL_RESEARCH", "TASK_")):
        return "research"
    if stage.startswith(("GAP_DETECTION", "RELEVANCE_")):
        return "gap_detection"
    if stage.startswith("WRITER"):
        return "writer"
    if stage.startswith("CITATION"):
        return "citation"
    return "planning"


def phase_progress(run, state):
    stages = state.completed_stages if state else set()
    options = state.phase_options if state else {}
    markers = dict(zip(PHASES, ("PLAN_CREATED", "INITIAL_RESEARCH_COMPLETED", "INITIAL_RESEARCH_COMPLETED",
                               "GAP_DETECTION_COMPLETED", "RERESEARCH_COMPLETED", "WRITER_COMPLETED", "CITATION_COMPLETED")))
    enabled = {"verification": options.get("verify_claims", True), "gap_detection": options.get("detect_gaps", True),
               "reresearch": options.get("reresearch", True), "writer": options.get("write_report", True),
               "citation": options.get("write_report", True)}
    active = phase_for_stage(run.current_stage)
    started = {
        "research": bool(state and (state.task_results or (state.research_plan and any(t.status != "pending" for t in state.research_plan.tasks)))),
        "verification": bool(state and state.verification_results),
    }
    results = []
    for phase in PHASES:
        status = "pending"
        if not enabled.get(phase, True):
            status = "skipped"
        elif markers[phase] in stages:
            status = "completed"
        elif phase == active and run.current_stage != "RUN_COMPLETED":
            status = {"RUNNING": "running", "FAILED": "failed", "INTERRUPTED": "interrupted"}.get(run.status.value, "pending")
        elif started.get(phase):
            status = "running" if run.status.value == "RUNNING" else "interrupted"
        results.append(PhaseProgress(phase=phase, status=status))
    return results
