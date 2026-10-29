"""Versioned, storage-independent DTOs and ports shared by the two domains."""

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Identifier = str


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scope(DTO):
    tenant_id: str
    project_id: str
    connector_id: str
    middleware_id: str
    origin_namespace: str
    actor_ids: tuple[str, ...]
    permissions: tuple[str, ...] = ("write", "read")


class Actor(DTO):
    external_id: str = Field(min_length=1, max_length=256)
    role: Literal["user", "assistant", "system", "tool", "service"]
    display_name: str | None = Field(default=None, max_length=256)


class ContentBlock(DTO):
    block_id: str = Field(min_length=1, max_length=256)
    type: Literal["text", "tool_result"]
    text: str = Field(min_length=1, max_length=100_000)
    language: str | None = None


class DeletionTarget(DTO):
    source_stream_id: str = Field(min_length=1, max_length=256)
    source_event_id: str = Field(min_length=1, max_length=256)
    entity_type: Literal["message", "document"]
    target_revision: None = None


class MemoryEvent(DTO):
    schema_version: Literal["1.1"]
    delivery_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=256)
    connector_id: str = Field(min_length=1, max_length=256)
    middleware_id: str = Field(min_length=1, max_length=256)
    origin_namespace: str = Field(min_length=1, max_length=256)
    source_stream_id: str = Field(min_length=1, max_length=256)
    source_event_id: str = Field(min_length=1, max_length=256)
    source_revision: str = Field(min_length=1, max_length=256)
    source_sequence: int | None = Field(default=None, ge=0)
    expected_previous_revision: str | None = None
    event_type: Literal[
        "message.upsert", "message.delete", "document.upsert", "document.delete", "tool.result"
    ]
    conversation_id: str | None = None
    actor: Actor
    occurred_at: AwareDatetime | None
    source_updated_at: AwareDatetime | None = None
    source_timezone: str | None = None
    time_precision: Literal["millisecond", "second", "minute", "day", "unknown"] = "unknown"
    content: tuple[ContentBlock, ...] = Field(default=(), max_length=128)
    deletion_of: DeletionTarget | None = None
    reply_to: str | None = None
    trace_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    backfill: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    mapping_revision: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_event(self):
        deleting = self.event_type.endswith(".delete")
        if deleting:
            if self.content or not self.deletion_of:
                raise ValueError("Delete requires deletion_of and no content")
            if self.deletion_of.entity_type != self.event_type.split(".")[0]:
                raise ValueError("Delete type and target type differ")
        elif not self.content or self.deletion_of:
            raise ValueError("Upsert/tool.result requires content and no deletion_of")
        if len({b.block_id for b in self.content}) != len(self.content):
            raise ValueError("block_id must be unique in an event")
        if self.occurred_at is None and self.time_precision != "unknown":
            raise ValueError("Unknown occurrence time requires unknown precision")
        if sum(len(b.text) for b in self.content) > 200_000:
            raise ValueError("Event text exceeds limit")
        return self


class Receipt(DTO):
    schema_version: Literal["1.2"] = "1.2"
    request_id: str
    delivery_id: str
    status: Literal["committed", "duplicate"]
    event_id: str
    tenant_id: str
    core_instance_id: str
    route_epoch: int = 1
    log_id: str
    commit_seq: int
    received_at: datetime
    content_hash: str
    canonicalization_version: str = "nfc-json-v1"
    processing_ref: str


class SourceRecord(DTO):
    event_id: str
    revision: str
    log_id: str
    commit_seq: int
    text: str
    occurred_at: datetime | None
    received_at: datetime
    conversation_id: str | None
    entity_key: str


class Change(DTO):
    schema_version: Literal["1.0"] = "1.0"
    change_id: str
    log_id: str
    commit_seq: int
    event_id: str
    revision: str
    entity_key: str
    operation: Literal["upsert", "delete"]


class Validation(DTO):
    allowed: tuple[str, ...]
    validation_revision: int
    validated_at: datetime


class Capability(DTO):
    capability: str
    generation: str | None
    log_id: str
    covered_commit_seq: int
    status: Literal["ready", "catching_up", "blocked_dependency", "failed", "unavailable"]
    observed_at: datetime
    reason: str | None = None


class IndexManifest(DTO):
    schema_version: Literal["1.0"] = "1.0"
    generation: str
    retrieval_type: Literal["lexical"] = "lexical"
    content_version: str = "raw-text-v1"
    chunking_version: str = "whole-event-v1"
    tokenizer_version: str
    embedding_model: None = None
    vector_space: None = None
    dimensions: None = None
    distance: None = None
    filter_schema: str = "tenant-project-conversation-v1"
    source_log_binding: str = "per-project-watermark"
    source_types: tuple[str, ...] = ("message", "document", "tool")


class Budget(DTO):
    max_evidence_items: int = Field(default=12, ge=1, le=40)
    max_context_tokens: int = Field(default=8000, ge=64, le=32000)
    recent_limit: int = Field(default=100, ge=1, le=1000)


class SearchRequest(DTO):
    project_id: str
    query: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None
    time_mode: Literal["current"] = "current"
    log_id: str | None = None
    min_commit_seq: int = Field(default=0, ge=0)
    consistency_mode: Literal["best_effort", "at_least"] = "best_effort"
    wait_timeout_ms: int = Field(default=500, ge=0, le=5000)
    required_capabilities: tuple[Literal["lexical", "vector", "facts"], ...] = ("lexical",)
    optional_capabilities: tuple[Literal["lexical", "vector", "facts"], ...] = ("vector",)
    budget: Budget = Field(default_factory=Budget)
    include_sources: Literal[True] = True


class Candidate(DTO):
    event_id: str
    revision: str
    rank: int
    route: str


class CheckpointWrite(DTO):
    checkpoint_id: str = Field(min_length=1, max_length=256)
    workflow_version: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256)
    state: dict[str, Any]


class MemoryReadPort(Protocol):
    def position(self, scope: Scope) -> tuple[str, int]: ...
    def read(self, scope: Scope, event_id: str) -> SourceRecord | None: ...
    def recent(
        self, scope: Scope, after: int, through: int, limit: int
    ) -> tuple[list[SourceRecord], bool]: ...


class MemoryChangeFeed(Protocol):
    def changes(self, scope: Scope, after: int, limit: int) -> list[Change]: ...


class EvidenceValidationPort(Protocol):
    def validate(self, scope: Scope, candidates: list[Candidate]) -> Validation: ...


class SearchIndexReader(Protocol):
    def status(self, scope: Scope, log_id: str, target: int) -> Capability: ...
    def search(
        self, scope: Scope, query: str, limit: int, conversation_id: str | None
    ) -> list[Candidate]: ...


class SearchIndexWriter(Protocol):
    def watermark(self, scope: Scope, log_id: str) -> int: ...
    def apply(self, scope: Scope, change: Change, source: SourceRecord | None) -> None: ...


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
