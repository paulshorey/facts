"""Versioned, source-preserving contracts for the organized collection."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VERSION = "organized.v1"
PROMPT_VERSION = "organize-v1"
TAXONOMY_VERSION = "facets-v1"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DateValue(Contract):
    role: str = "unknown"
    original_expression: str
    start: str | None = None
    end: str | None = None
    precision: Literal["day", "month", "year", "range", "unknown"] = "unknown"
    calendar: Literal["calendar", "fiscal", "unknown"] = "unknown"
    input_path: str = ""


class Measurement(Contract):
    metric: str = "unspecified"
    value: str | None = None
    low: str | None = None
    high: str | None = None
    comparator: str = "unknown"
    original_expression: str
    original_unit: str | None = None
    unit: str | None = None
    scale: str | None = None
    period: str | None = None
    basis: str | None = None
    value_kind: str = "unknown"
    qualifier: str | None = None
    input_path: str = ""


class EntityMention(Contract):
    name: str
    entity_id: str | None = None
    entity_type: str = "unknown"
    role: str = "mentioned"
    input_path: str = ""
    canonical_name: str | None = None
    aliases: list[str] = Field(default_factory=list)


class Attribution(Contract):
    name: str
    role: Literal["publisher", "byline", "estimate_by", "speaker", "reported_by"]
    input_path: str = ""


class Observation(Contract):
    schema_version: str = VERSION
    id: str
    topic: str
    record_type: Literal["observation", "source_reference"] = "observation"
    content_role: str = "substantive"
    kind: str = "statement"
    subtype: str = "unspecified"
    title: str
    detail: str = ""
    original_text: str = ""
    input_ids: list[str]
    dates: list[DateValue] = Field(default_factory=list)
    entities: list[EntityMention] = Field(default_factory=list)
    attribution: list[Attribution] = Field(default_factory=list)
    measurements: list[Measurement] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    original_tags: list[str] = Field(default_factory=list)
    temporal_context: str = "unknown"
    references: list[dict] = Field(default_factory=list)
    series_ids: list[str] = Field(default_factory=list)
    field_lineage: dict[str, list[str]] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    processing: dict = Field(default_factory=dict)


class ValueColumn(Contract):
    column: str
    metric: str
    period: str | None = None
    period_role: str = "unknown"
    unit: str | None = None
    scale: str | None = None
    unit_evidence: str | None = None
    basis: str | None = None
    value_kind: str = "level"


class TableMapping(Contract):
    orientation: Literal["wide", "long", "treasury", "metadata", "unresolved"]
    header_rows: list[str] = Field(default_factory=list)
    label_columns: list[str] = Field(default_factory=list)
    dimension_columns: list[str] = Field(default_factory=list)
    period_column: str | None = None
    value_columns: list[ValueColumn] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SemanticDate(Contract):
    expression: str
    role: str
    input_path: str


class SemanticMeasurement(Contract):
    expression: str
    metric: str
    unit: str | None = None
    period: str | None = None
    basis: str | None = None
    value_kind: str = "unknown"
    input_path: str


class SemanticPart(Contract):
    kind: Literal["event", "statement", "statistic", "quote", "document", "example", "resource"]
    subtype: str
    categories: list[str]
    evidence_paths: list[str]
    entities: list[EntityMention]
    attribution: list[Attribution]
    dates: list[SemanticDate]
    measurements: list[SemanticMeasurement]
    # A part selects supplied text; it does not write a new headline or summary.
    title_path: str = "/title"
    detail_path: str = "/detail"


class SemanticResult(Contract):
    input_id: str
    parts: list[SemanticPart] = Field(min_length=1, max_length=20)


class SemanticBatch(Contract):
    results: list[SemanticResult]


class GroupDecision(Contract):
    input_id: str
    candidate_id: str | None = None
    relation: Literal["same_occurrence", "related_subject", "no_match"]


class GroupBatch(Contract):
    results: list[GroupDecision]


class SeriesDefinition(Contract):
    schema_version: str = VERSION
    id: str
    topic: str
    record_type: Literal["series"] = "series"
    content_role: str = "dataset"
    kind: str = "series"
    subtype: str
    title: str
    detail: str
    input_ids: list[str]
    dataset_id: str
    metric: str
    basis: str | None
    unit: str | None
    scale: str | None
    unit_evidence: str | None
    dimensions: dict
    hierarchy: list[str]
    row_role: str
    categories: list[str]
    references: list[dict]
    values_artifact: str
    dates: list[DateValue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    processing: dict


class EntityDefinition(Contract):
    schema_version: str = VERSION
    id: str
    name: str
    entity_type: str
    aliases: list[str] = Field(default_factory=list)
    alias_evidence: list[dict] = Field(default_factory=list)


class GroupDefinition(Contract):
    schema_version: str = VERSION
    id: str
    topic: str
    group_kind: Literal["occurrence", "subject", "comparison"]
    title: str
    member_ids: list[str]
    observation_count: int
    anchor_id: str | None = None
    category: str | None = None
    source_url_count: int | None = None
    related_observation_ids: list[str] = Field(default_factory=list)
