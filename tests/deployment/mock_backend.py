"""Test-only app: real pipeline, validators, citations and SQLite; offline inputs.

Mounted only by the opt-in deployment test. Production never imports this file.
"""

import json
from pathlib import Path
from time import sleep

from app.agents.relevance import RelevanceAssessor
from app.agents.report_grounding import ReportGroundingChecker
from app.agents.verifier import ClaimVerifier
from app.agents.writer import ResearchWriter
from app.api.main import create_app
from app.config import get_settings
from app.research.gap_detection import GapDetector
from app.research.orchestrator import ResearchOrchestrator
from app.schemas import (Claim, CompactResearchResult, Evidence, ResearchExecutionResult,
                         ResearchPlan, ResearchTask, Source, TaskEvidenceBundle)


class FixtureSupervisor:
    def create_plan(self, query):
        sleep(.15)
        return ResearchPlan(goal=query, tasks=[
            ResearchTask(id="D1", title="First dimension", question="Find first fact", description="Offline fixture"),
            ResearchTask(id="D2", title="Second dimension", question="Find second fact", description="Offline fixture"),
            ResearchTask(id="A", title="Analysis", question="Compare facts", description="Offline fixture",
                         task_type="analysis", depends_on=["D1", "D2"]),
            ResearchTask(id="S", title="Synthesis", question="Summarize facts", description="Offline fixture",
                         task_type="synthesis", depends_on=["A"]),
        ])


class FixtureResearcher:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    def research(self, task, *, goal, **kwargs):
        with (self.data_dir / "fixture-executions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"goal": goal, "task_id": task.id}) + "\n")
        # The container test kills this worker after D1/D2 checkpoints exist.
        # The release file is then created in the same persistent test volume.
        if goal == "deployment pause" and task.id == "A":
            while not (self.data_dir / "fixture-release").exists():
                sleep(.1)
        sleep(.15)
        fact = f"Offline deployment fixture fact for task {task.id}."
        source = Source(task_id=task.id, title="Offline deployment fixture", url="https://example.org/deployment-fixture")
        evidence = Evidence(task_id=task.id, source_id=source.source_id, content=fact)
        claim = Claim(task_id=task.id, text=fact, evidence_ids=[evidence.evidence_id])
        return ResearchExecutionResult(memo=fact, tools_used=[], compact=CompactResearchResult(task_id=task.id, summary=fact),
            evidence_bundle=TaskEvidenceBundle(task_id=task.id, sources=[source], evidence=[evidence], claims=[claim]))


class FixtureModel:
    def __init__(self, status=None):
        self.status = status

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[1].text)
        if "items" in payload:
            return {"results": [{"item_index": item["item_index"], "status": "SUPPORTED", "reason": "Offline fixture."}
                                for item in payload["items"]]}
        if self.status:
            return {"results": [{"claim_id": item["claim_id"], "status": self.status, "reason": "The fixture claim equals its evidence."}
                                for item in payload["claims"]]}
        paragraphs = [{"text": item["text"], "claim_ids": [item["claim_id"]]} for item in payload["accepted_claims"]]
        return {"title": "Offline deployment fixture report", "scope": payload["goal"],
                "executive_summary": paragraphs[:2], "key_findings": paragraphs,
                "sections": [{"heading": "研究发现", "paragraphs": paragraphs}],
                "conclusion": paragraphs[:1], "limitations": []}


def create_smoke_app(*, settings=None):
    cfg = settings or get_settings()

    def factory(*, run_store):
        return ResearchOrchestrator(FixtureSupervisor(), FixtureResearcher(Path(run_store.path).parent),
            verifier=ClaimVerifier(FixtureModel("SUPPORTED"), settings=cfg),
            gap_detector=GapDetector(RelevanceAssessor(FixtureModel("ALIGNED"), settings=cfg)),
            writer=ResearchWriter(FixtureModel(), settings=cfg,
                                  grounding_checker=ReportGroundingChecker(FixtureModel(), settings=cfg)),
            max_concurrency=2, run_store=run_store)

    return create_app(settings=cfg, pipeline_factory=factory)


app = create_smoke_app()
