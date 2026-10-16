"""Disjoint model-local reference labels, mapped strictly to canonical IDs."""

from dataclasses import dataclass
import json
import re

from app.research.writing_context import WritingContext
from app.schemas.report import ResearchReport


class WriterReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class WriterReferences:
    text: str
    claims: dict[str, str]
    partial: dict[str, str]
    gaps: dict[str, str]
    literal_labels: frozenset[str]

    @classmethod
    def build(cls, context: WritingContext):
        payload = context.payload
        maps = []
        for field, key, prefix in (("accepted_claims", "claim_id", "C"),
                                   ("partial_claims_for_limitations_only", "claim_id", "P"),
                                   ("open_gaps", "gap_id", "G")):
            mapping = {}
            for index, item in enumerate(payload[field], 1):
                label = f"{prefix}{index}"
                mapping[label] = item[key]
                item[key] = label
            maps.append(mapping)
        # Literal terms such as a scientific C1 category are allowed when they
        # actually occur in the supplied facts/goal, not as invented trace labels.
        literals = re.findall(r"\b[CPG][1-9]\d*\b", "\n".join([
            context.goal, *(item.text for item in context.accepted_claims),
            *(item["text"] for item in payload["partial_claims_for_limitations_only"])]))
        return cls(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                   *maps, frozenset(literals))

    def decode(self, report: ResearchReport) -> ResearchReport:
        data = report.model_dump()

        def references(item, field, mapping, path):
            wrong = [key for key in item[field] if key not in mapping]
            if wrong:
                raise WriterReferenceError(
                    f"{path}.{field}: unknown or non-accepted reference labels {wrong[:5]}; "
                    f"allowed={list(mapping)}")
            item[field] = [mapping[key] for key in item[field]]

        texts = [data["title"], data["scope"]]
        for field in ("executive_summary", "key_findings", "conclusion"):
            for index, item in enumerate(data[field]):
                references(item, "claim_ids", self.claims, f"{field}.{index}")
                texts.append(item["text"])
        for index, section in enumerate(data["sections"]):
            texts.append(section["heading"])
            for offset, item in enumerate(section["paragraphs"]):
                references(item, "claim_ids", self.claims, f"sections.{index}.paragraphs.{offset}")
                texts.append(item["text"])
        for index, item in enumerate(data["limitations"]):
            references(item, "gap_ids", self.gaps, f"limitations.{index}")
            references(item, "partial_claim_ids", self.partial, f"limitations.{index}")
            texts.append(item["text"])
        for text in texts:
            leaked = set(re.findall(r"\b[CPG][1-9]\d*\b", text)) - self.literal_labels
            if leaked:
                raise WriterReferenceError(f"Writer prose leaked internal reference labels: {sorted(leaked)}")
        return ResearchReport.model_validate(data)
