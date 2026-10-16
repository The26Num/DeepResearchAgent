"""Incremental JSONL, atomic summaries and secret-safe experiment artifacts."""

import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

from dotenv import dotenv_values

from evaluation.models import ComparisonResult


class Redactor:
    def __init__(self, settings=None):
        self.secrets = set()
        values = {**dotenv_values('.env'), **os.environ}
        if settings is not None:
            values.update({name: getattr(settings, name) for name in type(settings).model_fields
                           if any(word in name.lower() for word in ("api_key", "password", "secret", "authorization"))})
        self.secrets.update(value for key, value in values.items() if isinstance(value, str) and value
                            and any(word in key.lower() for word in ("api_key", "password", "secret", "authorization")))

    def clean(self, value):
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;}]+", r"\1[REDACTED]", value)
            return re.sub(r"(?i)((?:api[_-]?key|token|password|secret)=)[^&\s]+", r"\1[REDACTED]", value)
        if isinstance(value, dict):
            return {key: self.clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(item) for item in value]
        return value


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def numeric_metrics(result):
    values = result.metrics.flat()
    if result.judge_result:
        judge = result.judge_result
        values.update({"judge." + name: getattr(judge, name) for name in
                       ("goal_relevance", "coverage", "groundedness", "clarity")})
        values["judge.dimension_coverage_score"] = judge.dimension_coverage_score
        values["judge.unsupported_statement_rate"] = judge.unsupported_statement_rate
    return values


def compare_results(case_id, full, baseline):
    left, right = numeric_metrics(full), numeric_metrics(baseline)
    keys = left.keys() | right.keys()
    delta = {key: left[key] - right[key] if isinstance(left.get(key), (int, float))
             and not isinstance(left[key], bool) and isinstance(right.get(key), (int, float))
             and not isinstance(right[key], bool) else None for key in sorted(keys)}
    return ComparisonResult(case_id=case_id, full_status=full.status, baseline_status=baseline.status, full_run_id=full.run_id,
                            full={key: left.get(key) for key in sorted(keys)},
                            baseline={key: right.get(key) for key in sorted(keys)}, delta=delta)


def aggregate(results):
    by_variant = {}
    for variant in sorted({item.variant for item in results}):
        group = [item for item in results if item.variant == variant]
        completed = [item for item in group if item.status == "COMPLETED"]
        samples = {}
        for item in completed:
            for key, value in numeric_metrics(item).items():
                if isinstance(value, (int, float, bool)):
                    samples.setdefault(key, []).append(float(value))
        keys = {key for item in group for key, value in numeric_metrics(item).items()
                if value is None or isinstance(value, (int, float, bool))}
        by_variant[variant] = {"runs_total": len(group), "runs_completed": len(completed),
            "runs_failed": sum(item.status == "FAILED" for item in group),
            "runs_interrupted": sum(item.status == "INTERRUPTED" for item in group),
            "means": {key: sum(samples[key]) / len(samples[key]) if key in samples else None for key in sorted(keys)},
            "metric_sample_count": {key: len(samples.get(key, [])) for key in sorted(keys)}}
        # Failed runs still consume budget. Keep their observed costs separately
        # from completed-run quality/cost means, without making up missing usage.
        costs = {}
        for key in ("observed_llm_calls", "observed_total_tokens", "web_search_calls", "fetch_webpage_calls"):
            available = [item.metrics.efficiency.get(key) for item in group if item.metrics.efficiency.get(key) is not None]
            costs[key] = {"sum": sum(available) if available else None, "sample_count": len(available)}
        costs["judge_llm_calls"] = sum(item.judge_usage.get("llm_calls", 0) for item in group)
        costs["judge_observed_tokens"] = sum(item.judge_usage.get("total_tokens") or 0 for item in group)
        costs["judge_usage_responses"] = sum(item.judge_usage.get("usage_responses", 0) for item in group)
        costs["runtime_seconds"] = sum(item.metrics.timing.get("total_runtime_seconds") or 0 for item in group)
        by_variant[variant]["all_runs_observed_costs"] = costs
    return by_variant


class EvaluationOutput:
    def __init__(self, root="workspace/evaluations", *, redactor=None):
        self.eval_id = uuid4().hex
        self.path = Path(root) / self.eval_id
        self.path.mkdir(parents=True, exist_ok=False)
        self.redactor = redactor or Redactor()
        self.entries, self.results, self.comparisons = [], [], []

    def save_json(self, name, data):
        target = self.path / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(json.dumps(self.redactor.clean(data), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(target)

    def save_report(self, case_id, variant, report):
        target = self.path / f"{case_id}.{variant}.md"
        target.write_text(self.redactor.clean(report), encoding="utf-8")
        return str(target)

    def append_case(self, case, results):
        primary = results[0]
        entry = {**primary.model_dump(mode="json"), "question": case.question,
                 "status": "COMPLETED" if all(item.status == "COMPLETED" for item in results) else "FAILED",
                 "results": {item.variant: item.model_dump(mode="json") for item in results}}
        with (self.path / "cases.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(self.redactor.clean(entry), ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
        self.entries.append(entry)
        self.results.extend(results)
        if len(results) == 2:
            self.comparisons.append(compare_results(case.case_id, results[0], results[1]).model_dump(mode="json"))
        self.summarize()

    def summarize(self):
        summary = {"eval_id": self.eval_id, "updated_at": datetime.now(timezone.utc).isoformat(),
                   "cases_total": len(self.entries), "cases_completed": sum(item["status"] == "COMPLETED" for item in self.entries),
                   "cases_failed": sum(item["status"] != "COMPLETED" for item in self.entries), "variants": aggregate(self.results)}
        self.save_json("summary.json", summary)
        if self.comparisons:
            self.save_json("comparison.json", self.comparisons)
        rows = []
        for entry in self.entries:
            row = {"case_id": entry["case_id"], "status": entry["status"]}
            for variant, raw in entry["results"].items():
                from evaluation.models import EvaluationResult
                result = EvaluationResult.model_validate(raw)
                row[variant + ".run_id"] = result.run_id
                for key, value in numeric_metrics(result).items():
                    if value is None or isinstance(value, (int, float, bool, str)):
                        row[variant + "." + key] = value
            rows.append(self.redactor.clean(row))
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with (self.path / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return summary
