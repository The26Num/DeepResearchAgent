"""Provider-specific JSON transport and strict data-instance parsing.

Qwen3.5 on Alibaba Cloud supports JSON Object in non-thinking mode, rather
than native JSON Schema. Other providers retain their existing native path.
"""

import json
from urllib.parse import urlsplit


class SchemaEchoError(ValueError):
    """The response contains a format definition instead of task result data."""


def prefers_json_object(model) -> bool:
    host = urlsplit(str(getattr(model, "openai_api_base", "") or "")).hostname or ""
    name = str(getattr(model, "model_name", "") or "").lower()
    return host.endswith(".aliyuncs.com") and name.startswith("qwen3.5-")


def json_object_options(model) -> dict:
    if not prefers_json_object(model):
        return {}
    return {"response_format": {"type": "json_object"},
            "extra_body": {**(getattr(model, "extra_body", None) or {}), "enable_thinking": False}}


def _shape(schema, root, depth=0, field=None):
    """A small illustrative data object, never a source of factual content."""
    if depth > 8:
        return "<value>"
    if "$ref" in schema:
        definition = schema["$ref"].removeprefix("#/$defs/")
        return _shape(root.get("$defs", {}).get(definition, {}), root, depth + 1, field)
    if "anyOf" in schema:
        schema = next((item for item in schema["anyOf"] if item.get("type") != "null"), schema["anyOf"][0])
        return _shape(schema, root, depth + 1, field)
    if schema.get("type") == "object":
        return {key: _shape(value, root, depth + 1, key) for key, value in schema.get("properties", {}).items()}
    if schema.get("type") == "array":
        return [] if schema.get("maxItems") == 0 else [_shape(schema.get("items", {}), root, depth + 1, field)]
    if "enum" in schema:
        if field in {"status", "task_type"}:
            return "<" + " | ".join(str(value) for value in schema["enum"]) + ">"
        return schema["enum"][0]
    return {"integer": 0, "number": 0, "boolean": False, "null": None}.get(schema.get("type"), "<fill from task input>")


def json_instance_instructions(schema: dict) -> str:
    shape = _shape(schema, schema)
    # Decision batches have an exact label set. Showing just C1 can suggest that
    # the remaining entries are optional when JSON mode does not enforce Schema.
    results = schema.get("properties", {}).get("results", {})
    item = results.get("items", {})
    if "$ref" in item:
        item = schema.get("$defs", {}).get(item["$ref"].removeprefix("#/$defs/"), {})
    labels = item.get("properties", {}).get("claim_id", {}).get("enum", [])
    if labels and results.get("minItems") == len(labels):
        shape["results"] = [{**shape["results"][0], "claim_id": label} for label in labels]
    example = json.dumps(shape, ensure_ascii=False, separators=(",", ":"))
    reference_fields = {"claim_id", "claim_ids", "partial_claim_ids", "gap_ids", "group_id"}
    allowed = {}

    def references(node):
        if not isinstance(node, dict):
            return
        for key, prop in node.get("properties", {}).items():
            if key in reference_fields:
                target = prop.get("items", prop)
                if "enum" in target:
                    allowed[key] = target["enum"]
                elif prop.get("maxItems") == 0:
                    allowed[key] = []
        for value in node.values():
            if isinstance(value, dict):
                references(value)
            elif isinstance(value, list):
                for child in value:
                    references(child)

    references(schema)
    whitelist = ("\nREFERENCE WHITELISTS BY OUTPUT FIELD: " + json.dumps(allowed, separators=(",", ":"))
                 + ". These pools are distinct. A partial_claim_ids value is not a legal claim_ids value. "
                   "Every factual paragraph must use only the claim_ids whitelist.\n") if allowed else ""
    return (
        "\nReturn one JSON DATA INSTANCE containing the actual task results. "
        "The example below shows only the output shape; replace every placeholder and example value "
        "using the supplied task data. Empty lists are allowed only when the schema permits them. "
        "Copy reference IDs literally from the allowed enums; do not rename IDs or output placeholders. "
        "Do not copy the schema, add schema-definition keys, or wrap the answer in another object.\n" + whitelist +
        "OUTPUT DATA SHAPE (illustration, not an answer):\n" + example
        + "\nVALIDATION RULES (JSON Schema; these rules are not output data):\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )


def parse_json_instance(content: str):
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    result = json.loads(text)
    if isinstance(result, dict) and ("$defs" in result or "$schema" in result
                                   or result.get("type") == "object" and "properties" in result):
        raise SchemaEchoError("Model returned a JSON Schema instead of result data; no results published.")
    return result
