"""Fictional incident application schema and independent artifact verifier."""

import re
from typing import Annotated, Literal

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Incident(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9-]{0,31}$")]
    title: Annotated[str, Field(min_length=1, max_length=160)]
    severity: Literal["low", "medium", "high"]


class IncidentBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    records: Annotated[list[Incident], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def unique(self) -> "IncidentBatch":
        if len({record.id for record in self.records}) != len(self.records):
            raise ValueError("Duplicate incident IDs")
        if len(rfc8785.dumps(self.model_dump())) > 8192:
            raise ValueError("Incident input exceeds 8192 bytes")
        return self


class IncidentArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    index: list[Incident]
    summary: str


def verify(batch: IncidentBatch, result: IncidentArtifacts) -> None:
    """Check independent observable criteria, not the model's assessment of its answer."""
    expected = {record.id: record for record in batch.records}
    if len(result.index) != len(expected) or {r.id: r for r in result.index} != expected:
        raise ValueError("Index is not the authorized input set")
    references = re.findall(r"^- \[([A-Z][A-Z0-9-]{0,31})\]", result.summary, re.MULTILINE)
    if len(references) != len(expected) or set(references) != set(expected):
        raise ValueError("Summary references do not cover exactly the input set")
    if len(result.summary.encode()) > 8192:
        raise ValueError("Summary exceeds output limit")
