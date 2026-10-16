"""Evidence-grounded prose with internal provenance; no citation formatting."""

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_REPORT_TITLE_CHARS = 300
MAX_REPORT_SCOPE_CHARS = 2000
MAX_REPORT_PARAGRAPH_CHARS = 2000
MAX_REPORT_HEADING_CHARS = 240
MAX_REPORT_LIMITATION_CHARS = 1000


class _ReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)


class ReportParagraph(_ReportModel):
    text: str = Field(min_length=1, max_length=MAX_REPORT_PARAGRAPH_CHARS)
    claim_ids: list[str] = Field(min_length=1, max_length=30)

    @field_validator("claim_ids")
    @classmethod
    def valid_claim_ids(cls, values):
        if any(not key.strip() for key in values) or len(values) != len(set(values)):
            raise ValueError("Paragraph claim_ids must be nonempty unique references.")
        return values


class Finding(ReportParagraph):
    pass


class ReportSection(_ReportModel):
    heading: str = Field(min_length=1, max_length=MAX_REPORT_HEADING_CHARS)
    paragraphs: list[ReportParagraph] = Field(min_length=1, max_length=6)


class ReportLimitation(_ReportModel):
    text: str = Field(min_length=1, max_length=MAX_REPORT_LIMITATION_CHARS)
    gap_ids: list[str] = Field(default_factory=list, max_length=45)
    partial_claim_ids: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_references(self):
        for refs in (self.gap_ids, self.partial_claim_ids):
            if any(not key.strip() for key in refs) or len(refs) != len(set(refs)):
                raise ValueError("Limitation references must be nonempty unique IDs.")
        return self


class ResearchReport(_ReportModel):
    title: str = Field(min_length=1, max_length=MAX_REPORT_TITLE_CHARS)
    executive_summary: list[ReportParagraph] = Field(max_length=4)
    scope: str = Field(min_length=1, max_length=MAX_REPORT_SCOPE_CHARS)
    key_findings: list[Finding] = Field(max_length=15)
    sections: list[ReportSection] = Field(max_length=8)
    limitations: list[ReportLimitation] = Field(max_length=30)
    conclusion: list[ReportParagraph] = Field(max_length=3)

    def factual_paragraphs(self) -> list[ReportParagraph]:
        return [*self.executive_summary, *self.key_findings,
                *(item for section in self.sections for item in section.paragraphs), *self.conclusion]

    @property
    def used_claim_ids(self) -> list[str]:
        return list(dict.fromkeys(key for item in self.factual_paragraphs() for key in item.claim_ids))
