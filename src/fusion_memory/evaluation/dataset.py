import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from fusion_memory.evaluation.contracts import (
    DatasetManifest,
    Episode,
    EvidenceUnit,
    FixtureEvent,
    GoldQuestion,
    Question,
)

FILES = {
    "public/episodes.jsonl": Episode,
    "public/events.jsonl": FixtureEvent,
    "public/questions.jsonl": Question,
    "evaluator_only/evidence_units.jsonl": EvidenceUnit,
    "evaluator_only/gold_questions.jsonl": GoldQuestion,
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def unique(items, field):
    result = {getattr(item, field): item for item in items}
    if len(result) != len(items):
        raise ValueError(f"Duplicate {field}")
    return result


@dataclass(frozen=True)
class Dataset:
    manifest: DatasetManifest
    manifest_hash: str
    episodes: dict[str, Episode]
    events: dict[str, FixtureEvent]
    questions: dict[str, Question]
    units: dict[str, EvidenceUnit]
    gold: dict[str, GoldQuestion]

    def prefix(self, question: Question) -> tuple[FixtureEvent, ...]:
        return tuple(
            sorted(
                (
                    e
                    for e in self.events.values()
                    if e.episode_id == question.episode_id and e.replay_seq <= question.cutoff_replay_seq
                ),
                key=lambda e: (e.fixture_received_at, e.replay_seq),
            )
        )


def entity(event: FixtureEvent, subjects: dict) -> tuple:
    p, scope = event.payload, subjects[event.auth_subject_ref]
    target = p.deletion_of
    return (
        scope.tenant_id,
        scope.project_id,
        p.origin_namespace,
        target.source_stream_id if target else p.source_stream_id,
        target.source_event_id if target else p.source_event_id,
        target.entity_type if target else p.event_type.split(".")[0],
    )


def live_sources(events, subjects) -> set[str]:
    heads, deleted = {}, set()
    for event in events:
        key = entity(event, subjects)
        if event.payload.deletion_of:
            deleted.add(key)
            heads.pop(key, None)
        elif key not in deleted:
            previous = heads.get(key)
            if previous and previous.payload.source_revision == event.payload.source_revision:
                continue  # Duplicate deliveries do not create another source revision.
            heads[key] = event
    return {e.fixture_event_id for e in heads.values()}


def load_dataset(root: Path) -> Dataset:
    root = root.resolve()
    raw = (root / "manifest.json").read_bytes()
    manifest = DatasetManifest.model_validate_json(raw)
    if set(manifest.files) != set(FILES):
        raise ValueError("Dataset must declare the exact public/evaluator-only file allowlist")
    parsed = {}
    for name, model in FILES.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Dataset path escapes its root")
        content = path.read_bytes()
        if digest(content) != manifest.files[name]:
            raise ValueError(f"Dataset hash mismatch: {name}")
        parsed[name] = [
            model.model_validate_json(line) for line in content.decode("utf-8").splitlines() if line.strip()
        ]
    dataset = Dataset(
        manifest,
        digest(raw),
        unique(parsed["public/episodes.jsonl"], "episode_id"),
        unique(parsed["public/events.jsonl"], "fixture_event_id"),
        unique(parsed["public/questions.jsonl"], "question_id"),
        unique(parsed["evaluator_only/evidence_units.jsonl"], "unit_id"),
        unique(parsed["evaluator_only/gold_questions.jsonl"], "question_id"),
    )
    validate_dataset(dataset)
    return dataset


def validate_revisions(d: Dataset):
    """Admit successful ingestion fixtures only; rejected-write fault scripts are a later contract."""
    heads, revisions, deliveries, deleted = {}, {}, {}, set()
    for event in sorted(d.events.values(), key=lambda e: (e.episode_id, e.replay_seq)):
        p = event.payload
        scope = d.manifest.subjects[event.auth_subject_ref]
        source = (
            event.episode_id,
            scope.tenant_id,
            scope.project_id,
            p.origin_namespace,
            p.source_stream_id,
            p.source_event_id,
        )
        revision = (*source, p.source_revision, p.event_type)
        delivery = (event.episode_id, scope.tenant_id, scope.project_id, p.connector_id, p.delivery_id)
        if delivery in deliveries and deliveries[delivery] != revision:
            raise ValueError("Fixture delivery ID identifies different revisions")
        deliveries[delivery] = revision
        semantic = {
            k: v
            for k, v in p.model_dump(mode="json").items()
            if k
            not in {
                "delivery_id",
                "connector_id",
                "middleware_id",
                "trace_id",
                "correlation_id",
                "causation_id",
                "backfill",
                "mapping_revision",
            }
        }
        if revision in revisions:
            if semantic != revisions[revision]:
                raise ValueError("Conflicting duplicate fixture revision")
            continue
        revisions[revision] = semantic
        key = (event.episode_id, *entity(event, d.manifest.subjects))
        if p.deletion_of:
            deleted.add(key)
            continue
        if key in deleted:
            raise ValueError("Fixture attempts to resurrect a deleted source")
        previous = heads.get(key)
        if previous:
            if p.expected_previous_revision is not None:
                if p.expected_previous_revision != previous.source_revision:
                    raise ValueError("Fixture revision CAS differs from its predecessor")
            elif p.source_sequence is None or previous.source_sequence is None:
                raise ValueError("Fixture revisions require CAS or an ordered source sequence")
            if (
                p.source_sequence is not None
                and previous.source_sequence is not None
                and p.source_sequence <= previous.source_sequence
            ):
                raise ValueError("Stale fixture revision requires a rejected-write fault contract")
        elif p.expected_previous_revision is not None:
            raise ValueError("Fixture revision predecessor does not exist")
        heads[key] = p


def validate_dataset(d: Dataset):
    if not d.questions or set(d.questions) != set(d.gold):
        raise ValueError("Every scheduled question needs exactly one Gold label")
    subjects = d.manifest.subjects
    timeline = {}
    for item in [*d.events.values(), *d.questions.values()]:
        if item.episode_id not in d.episodes or item.auth_subject_ref not in subjects:
            raise ValueError("Unknown episode or identity")
        key = (item.episode_id, item.replay_seq)
        if key in timeline:
            raise ValueError("replay_seq must disambiguate events and questions")
        timeline[key] = getattr(item, "fixture_received_at", None) or item.query_at
    for ep in d.episodes:
        times = [at for (episode, seq), at in sorted(timeline.items()) if episode == ep]
        if times != sorted(times):
            raise ValueError("Replay sequence contradicts the frozen arrival timeline")
    for event in d.events.values():
        scope, p = subjects[event.auth_subject_ref], event.payload
        if (
            any(
                getattr(scope, field) != getattr(p, field)
                for field in ("project_id", "connector_id", "middleware_id", "origin_namespace")
            )
            or p.actor.external_id not in scope.actor_ids
        ):
            raise ValueError("Fixture payload identity differs from its credential")
        if p.metadata:
            raise ValueError("Pilot rejects free-form metadata on public events")
    validate_revisions(d)
    clusters = {}
    for question in d.questions.values():
        cluster = d.gold[question.question_id].cluster_id
        if question.episode_id in clusters and clusters[question.episode_id] != cluster:
            raise ValueError("An episode cannot be split into independent statistical clusters")
        clusters[question.episode_id] = cluster
    for unit in d.units.values():
        if unit.fixture_event_id not in d.events:
            raise ValueError("Evidence refers to an unknown event")
        p = d.events[unit.fixture_event_id].payload
        block = next((b for b in p.content if b.block_id == unit.block_id), None)
        if (
            p.source_revision != unit.source_revision
            or block is None
            or unit.end > len(block.text)
            or unit.start >= unit.end
            or block.text[unit.start : unit.end] != unit.quote
        ):
            raise ValueError("Evidence must match the exact source revision and character span")
    for q in d.questions.values():
        prefix = d.prefix(q)
        if any(e.fixture_received_at > q.knowledge_cutoff_at for e in prefix):
            raise ValueError("Prefix includes future received information")
        allowed = [
            e
            for e in d.events.values()
            if e.episode_id == q.episode_id
            and e.replay_seq < q.replay_seq
            and e.fixture_received_at <= q.knowledge_cutoff_at
        ]
        if {e.fixture_event_id for e in allowed} != {e.fixture_event_id for e in prefix}:
            raise ValueError("Cutoff must cover the complete knowledge prefix")
        live = live_sources(prefix, subjects)
        scope = subjects[q.auth_subject_ref]
        label = d.gold[q.question_id]
        groups = [*label.gold_evidence_sets, *(g for a in label.answer_atoms for g in a.evidence_sets)]
        for group in groups:
            for unit_id in group:
                if unit_id not in d.units:
                    raise ValueError("Gold refers to an unknown evidence unit")
                unit = d.units[unit_id]
                event = d.events[unit.fixture_event_id]
                source_scope = subjects[event.auth_subject_ref]
                if (
                    event.episode_id != q.episode_id
                    or unit.fixture_event_id not in live
                    or (source_scope.tenant_id, source_scope.project_id)
                    != (scope.tenant_id, scope.project_id)
                    or q.project_id != scope.project_id
                    or (q.conversation_id and q.conversation_id != event.payload.conversation_id)
                ):
                    raise ValueError("Gold evidence is future, inactive, deleted or outside query scope")
        for atom in label.answer_atoms:
            if not all(
                any(text in "\n".join(d.units[u].quote for u in group) for text in atom.accepted_text)
                for group in atom.evidence_sets
            ):
                raise ValueError("Literal answer atom is not carried by its evidence")
        for group in label.gold_evidence_sets:
            if not all(
                any(set(support) <= set(group) for support in atom.evidence_sets)
                for atom in label.answer_atoms
            ):
                raise ValueError("A sufficient evidence set must support every required answer atom")
