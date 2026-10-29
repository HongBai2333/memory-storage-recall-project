"""Public replay inputs and evaluator-only labels are separate strict contracts."""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from fusion_memory.contracts import DTO, Budget, MemoryEvent, Scope


class Episode(DTO):
    episode_id: str = Field(min_length=1)
    description: str


class FixtureEvent(DTO):
    fixture_event_id: str = Field(min_length=1)
    episode_id: str
    replay_seq: int = Field(ge=1)
    fixture_received_at: AwareDatetime
    auth_subject_ref: str
    payload: MemoryEvent


class Question(DTO):
    question_id: str = Field(min_length=1)
    episode_id: str
    checkpoint_id: str
    replay_seq: int = Field(ge=1)
    query_at: AwareDatetime
    cutoff_replay_seq: int = Field(ge=0)
    knowledge_cutoff_at: AwareDatetime
    target_valid_at: AwareDatetime
    time_mode: Literal["current", "as_known_at", "as_of_event"] = "current"
    auth_subject_ref: str
    project_id: str
    conversation_id: str | None = None
    question: str = Field(min_length=1, max_length=2000)
    required_capabilities: tuple[str, ...] = ("raw",)
    consistency_mode: Literal["best_effort", "at_least"] = "best_effort"

    @model_validator(mode="after")
    def temporal_order(self):
        if self.cutoff_replay_seq >= self.replay_seq:
            raise ValueError("Question must follow its event prefix")
        if self.knowledge_cutoff_at > self.query_at:
            raise ValueError("Knowledge cutoff cannot be in the future")
        if self.time_mode == "current" and self.target_valid_at != self.query_at:
            raise ValueError("Current queries target query_at")
        return self


class EvidenceUnit(DTO):
    unit_id: str = Field(min_length=1)
    fixture_event_id: str
    source_revision: str
    block_id: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)


class AnswerAtom(DTO):
    atom_id: str
    accepted_text: tuple[str, ...] = Field(min_length=1)
    evidence_sets: tuple[tuple[str, ...], ...] = Field(min_length=1)


class GoldQuestion(DTO):
    question_id: str
    expected_action: Literal["answer", "insufficient_evidence", "deny", "pending"]
    answer_atoms: tuple[AnswerAtom, ...] = ()
    gold_evidence_sets: tuple[tuple[str, ...], ...] = ()
    forbidden_claims: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    cluster_id: str = Field(min_length=1)
    rubric: Literal["literal-extractive-v1"] = "literal-extractive-v1"

    @model_validator(mode="after")
    def valid_label(self):
        if self.expected_action == "answer":
            if not self.answer_atoms or not self.gold_evidence_sets:
                raise ValueError("Answer labels require atoms and sufficient evidence")
        elif self.answer_atoms or self.gold_evidence_sets:
            raise ValueError("Non-answer labels must not carry answer evidence")
        groups = list(self.gold_evidence_sets)
        for atom in self.answer_atoms:
            if any(not text.strip() for text in atom.accepted_text):
                raise ValueError("Empty answer atoms are forbidden")
            groups.extend(atom.evidence_sets)
        if any(not group or len(group) != len(set(group)) for group in groups):
            raise ValueError("Evidence sets must be nonempty and unique")
        if len({a.atom_id for a in self.answer_atoms}) != len(self.answer_atoms):
            raise ValueError("Duplicate answer atom")
        return self


class DatasetManifest(DTO):
    schema_version: Literal["eval-dataset-1.0"] = "eval-dataset-1.0"
    dataset_id: str
    revision: str
    split: Literal["development", "test"]
    license: str = Field(min_length=1)
    synthetic: bool
    annotation_status: Literal["synthetic_unreviewed", "human_reviewed"]
    files: dict[str, str]
    subjects: dict[str, Scope]


class Variant(DTO):
    variant_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    index_enabled: bool = True
    worker_delay_ms: int = Field(default=0, ge=0, le=3_600_000)


class Experiment(DTO):
    schema_version: Literal["eval-experiment-1.0"] = "eval-experiment-1.0"
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    modes: tuple[Literal["capability", "operational"], ...] = ("capability", "operational")
    operational_clock: Literal["simulated"] = "simulated"
    repetitions: int = Field(default=1, ge=1, le=20)
    variants: tuple[Variant, ...] = Field(min_length=1)
    budget: Budget = Field(default_factory=Budget)
    query_deadline_ms: int = Field(default=5000, ge=1, le=60_000)
    case_timeout_seconds: int = Field(default=30, ge=1, le=300)
    host: Literal["extractive-context-v1"] = "extractive-context-v1"
    rubric_revision: Literal["literal-extractive-v1"] = "literal-extractive-v1"
    bootstrap_repetitions: int = Field(default=2000, ge=100, le=10_000)
    random_seed: int = 101

    @model_validator(mode="after")
    def unique(self):
        if not self.modes or len(set(self.modes)) != len(self.modes):
            raise ValueError("Modes must be nonempty and unique")
        if len({v.variant_id for v in self.variants}) != len(self.variants):
            raise ValueError("Variant IDs must be unique")
        return self


class PublicCase(DTO):
    """The entire allowlisted stdin of the SUT process; no paths, Gold or tags."""

    subjects: dict[str, Scope]
    events: tuple[FixtureEvent, ...]
    question: Question
    variant: Variant
    mode: Literal["capability", "operational"]
    budget: Budget
    query_deadline_ms: int
