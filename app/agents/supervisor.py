import json
from datetime import date
from pathlib import Path

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from openai import BadRequestError
from pydantic import ValidationError

from app.schemas.plan import ResearchPlan, validate_plan_dependencies


_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "supervisor.md"


class Supervisor:
    def __init__(self, model: BaseChatModel) -> None:
        self.model = model
        self.system_prompt = _PROMPT.read_text(encoding="utf-8")

    def create_plan(self, query: str) -> ResearchPlan:
        if not query.strip():
            raise ValueError("Research query must not be empty.")

        dated_query = f"Current date: {date.today().isoformat()}\nResearch question: {query}"
        messages = [SystemMessage(content=self.system_prompt), HumanMessage(content=dated_query)]
        try:
            structured = self.model.with_structured_output(ResearchPlan, method="json_schema")
            plan = ResearchPlan.model_validate(structured.invoke(messages))
            self._validate_plan(plan)
            return plan
        except (NotImplementedError, TypeError, ValueError, OutputParserException, BadRequestError) as structured_error:
            # Some OpenAI-compatible models reject response_format=json_schema.
            try:
                fallback_messages = [
                    SystemMessage(content=self.system_prompt + "\nReturn only a JSON object matching this schema:\n" + json.dumps(ResearchPlan.model_json_schema(), ensure_ascii=False)),
                    HumanMessage(content=dated_query),
                ]
                response = self.model.invoke(fallback_messages)
                raw = response.text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                plan = ResearchPlan.model_validate_json(raw)
                self._validate_plan(plan)
                return plan
            except (ValueError, ValidationError, json.JSONDecodeError, IndexError, AttributeError) as parse_error:
                raise RuntimeError(
                    f"Supervisor could not parse a valid ResearchPlan. Structured output failed: {structured_error}. JSON fallback failed: {parse_error}"
                ) from parse_error
            except Exception as fallback_error:
                raise RuntimeError(
                    f"Supervisor could not create a ResearchPlan. Structured output failed: {structured_error}. JSON fallback failed: {fallback_error}"
                ) from fallback_error

    @staticmethod
    def _validate_plan(plan: ResearchPlan) -> None:
        if not 3 <= len(plan.tasks) <= 6:
            raise ValueError("ResearchPlan must contain 3 to 6 tasks.")
        for task in plan.tasks:
            if "task_type" not in task.model_fields_set:
                raise ValueError(f"Supervisor must explicitly set task_type for task {task.id}.")
        validate_plan_dependencies(plan)
        if any(task.status != "pending" for task in plan.tasks):
            raise ValueError("ResearchPlan tasks must start as pending.")
