"""One compact, tool-free batch request to fill selected research needs."""

import json
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from openai import BadRequestError
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.llm import bounded_output_options, create_verifier_llm
from app.research.reresearch import GapGroup
from app.research.runtime import ResearchRuntime
from app.schemas.claim import Claim
from app.schemas.plan import ResearchPlan
from app.schemas.reresearch import ReResearchPlan, ReResearchTask


class _TaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    group_id: str
    title: str = Field(min_length=1, max_length=180)
    description: str = Field(min_length=1, max_length=900)


class _BatchDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[_TaskDraft]


class ReResearchPlanner:
    def __init__(self, model=None, *, settings: Settings | None = None):
        self.model = model
        self.settings = settings or get_settings()
        self.prompt = (Path(__file__).resolve().parents[1] / "prompts" / "reresearch_planner.md").read_text(encoding="utf-8")
        self.last_calls = 0
        self._native_available = True

    def create_plan(self, goal: str, original: ResearchPlan, groups: list[GapGroup],
                    claims: list[Claim], *, round_id: int = 1) -> ReResearchPlan:
        self.last_calls = 0
        if not groups:
            return ReResearchPlan(round_id=round_id, selected_gap_ids=[], tasks=[])
        if (len(groups) > self.settings.max_reresearch_tasks_per_round
                or len({group.task_id for group in groups}) != len(groups)
                or len({group.group_id for group in groups}) != len(groups)):
            raise ValueError("Planner groups exceed the task budget or contain duplicate origins.")
        tasks = {task.id: task for task in original.tasks}
        related = {claim.claim_id: claim for claim in claims}
        payload = {"research_goal": goal, "groups": []}
        for group in groups:
            task = tasks[group.task_id]
            payload["groups"].append({
                "group_id": group.group_id,
                "original_task": {"title": task.title, "description": task.description},
                "gap_types": [item.value for item in group.gap_types],
                "missing_information": [text[:450] for text in group.missing_information],
                "related_claim_texts": [related[key].text[:350] for key in group.claim_ids[:3] if key in related],
            })
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(data) > self.settings.max_reresearch_planning_context_chars:
            raise ValueError("Supplemental planning context exceeds MAX_RERESEARCH_PLANNING_CONTEXT_CHARS.")
        schema = _BatchDraft.model_json_schema()
        schema["properties"]["results"].update(minItems=len(groups), maxItems=len(groups))
        schema["$defs"]["_TaskDraft"]["properties"]["group_id"]["enum"] = [group.group_id for group in groups]
        runtime = ResearchRuntime("Re-Research Planner", self.settings.reresearch_planning_timeout_seconds,
                                  self.settings.research_progress_interval_seconds)

        def work():
            if self.model is None:
                self.model = create_verifier_llm()
            messages = [SystemMessage(content=self.prompt), HumanMessage(content=data)]

            def invoke(model, request):
                options = bounded_output_options(self.model, self.settings.reresearch_planning_max_tokens)
                options["timeout"] = min(self.settings.model_request_timeout_seconds, runtime.remaining())
                if runtime.call_budget is not None:
                    runtime.call_budget.reserve("planner")
                self.last_calls += 1
                response = model.invoke(request, **options)
                runtime.remaining()
                return response

            response = None
            if self._native_available:
                try:
                    structured = self.model.with_structured_output(schema, method="json_schema")
                    response = invoke(structured, messages)
                    if response is None:
                        raise ValueError("Supplemental planner returned no structured tasks.")
                except (NotImplementedError, TypeError, BadRequestError) as error:
                    if isinstance(error, BadRequestError) and not any(
                        term in str(error).lower() for term in ("json_schema", "response_format")
                    ):
                        raise
                    runtime.remaining()
                    self._native_available = False
            if response is None:
                request = [SystemMessage(content=self.prompt + "\nReturn only JSON matching:\n"
                                          + json.dumps(schema, ensure_ascii=False)), messages[1]]
                raw = invoke(self.model, request)
                text = raw.text.strip()
                if text.startswith("```") and text.endswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                result = _BatchDraft.model_validate_json(text)
            else:
                result = _BatchDraft.model_validate(response)
            drafts = {item.group_id: item for item in result.results}
            if len(drafts) != len(result.results) or set(drafts) != {group.group_id for group in groups}:
                raise ValueError("Planner must return exactly one task for every selected group_id.")
            supplemental = []
            used_ids = set(tasks)
            for group in groups:
                draft = drafts[group.group_id]
                task_id = f"RR_{group.task_id}_{round_id}"
                while task_id in used_ids:
                    task_id += "_R"
                used_ids.add(task_id)
                supplemental.append(ReResearchTask(id=task_id, title=draft.title, question=draft.description,
                    description=draft.description, origin_task_id=group.task_id, origin_gap_ids=list(group.gap_ids)))
            return ReResearchPlan(round_id=round_id, tasks=supplemental,
                                  selected_gap_ids=[key for group in groups for key in group.gap_ids])

        def bounded_work():
            with runtime.operation("supplemental planning"):
                return work()

        return runtime.run(bounded_work)
