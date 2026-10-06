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
_DECOMPOSITION_PROMPT = _PROMPT.with_name("decomposition.md")


class Supervisor:
    def __init__(self, model: BaseChatModel) -> None:
        self.model = model
        self.system_prompt = _PROMPT.read_text(encoding="utf-8")
        self.decomposition_prompt = _DECOMPOSITION_PROMPT.read_text(encoding="utf-8")

    def create_plan(self, query: str) -> ResearchPlan:
        if not query.strip():
            raise ValueError("Research query must not be empty.")

        dated_query = (
            f"Current date: {date.today().isoformat()}\nResearch question: {query}\n"
            "规划要求：先根据这个问题动态选择互补、保留完整主题的独立搜索子问题。"
            "如果该问题涉及多方面研究进展，必须分别输出多个 discovery，不能仅用一个"
            "笼统的搜索任务覆盖整个问题；只有无法有意义拆分的窄问题才用单个 discovery。"
            "每个 discovery 的 depends_on=[]，再由有依赖的 analysis 和 synthesis 汇合结果。"
        )
        subproblems = self.model.invoke([
            SystemMessage(content=self.decomposition_prompt), HumanMessage(content=dated_query),
        ]).text.strip()
        if not subproblems:
            raise ValueError("Supervisor must identify research subproblems before creating a plan.")
        dated_query += (
            "\n独立搜索子问题：\n" + subproblems
            + "\n将上述互补子问题分别设为无依赖的 discovery，再添加分析和最终综合。"
            "如果子问题重复或偏离原始主题，应合并或修正；不要把独立子问题改成依赖别人的 analysis。"
            "总任务数必须为 2～6。多个 discovery 通常汇合到一个 analysis，再接一个 synthesis；"
            "不要机械地为每个 discovery 各添加一个 analysis 或 synthesis，导致超出任务预算。"
        )
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
        if not 2 <= len(plan.tasks) <= 6:
            raise ValueError("ResearchPlan must contain 2 to 6 tasks.")
        for task in plan.tasks:
            if "task_type" not in task.model_fields_set:
                raise ValueError(f"Supervisor must explicitly set task_type for task {task.id}.")
        validate_plan_dependencies(plan)
        for task in plan.tasks:
            if task.task_type == "discovery" and task.depends_on:
                raise ValueError(f"Discovery task {task.id} must be independent (depends_on=[]).")
            if task.task_type in ("analysis", "synthesis") and not task.depends_on:
                raise ValueError(f"{task.task_type.capitalize()} task {task.id} must depend on prior research results.")
        if any(task.status != "pending" for task in plan.tasks):
            raise ValueError("ResearchPlan tasks must start as pending.")
