"""Small transparent synthetic development fixture, not the 150 human-reviewed pilot."""

from datetime import datetime, timedelta
from pathlib import Path

from fusion_memory.contracts import Scope
from fusion_memory.evaluation.contracts import DatasetManifest, Experiment, Variant
from fusion_memory.evaluation.dataset import canonical, digest, load_dataset


def build_pilot(root: Path):
    root.mkdir(parents=True, exist_ok=False)
    (root / "public").mkdir()
    (root / "evaluator_only").mkdir()
    start = datetime.fromisoformat("2026-10-01T09:00:00+08:00")

    def at(seconds):
        return (start + timedelta(seconds=seconds)).isoformat()

    a = Scope(
        tenant_id="tenant-a",
        project_id="project-a",
        connector_id="fixture",
        middleware_id="eval",
        origin_namespace="fixture:a",
        actor_ids=("alice",),
    )
    b = a.model_copy(update={"tenant_id": "tenant-b", "origin_namespace": "fixture:b", "actor_ids": ("bob",)})
    subjects = {"alice": a, "bob": b}
    events, questions, units, labels = [], [], [], []

    def event(
        fid,
        ep,
        seq,
        seconds,
        text,
        subject="alice",
        source=None,
        revision="1",
        source_seq=1,
        conversation="chat",
        blocks=None,
    ):
        scope = subjects[subject]
        payload = {
            "schema_version": "1.1",
            "delivery_id": fid,
            "project_id": scope.project_id,
            "connector_id": scope.connector_id,
            "middleware_id": scope.middleware_id,
            "origin_namespace": scope.origin_namespace,
            "source_stream_id": conversation,
            "source_event_id": source or fid,
            "source_revision": revision,
            "source_sequence": source_seq,
            "event_type": "message.upsert",
            "conversation_id": conversation,
            "actor": {"external_id": subject, "role": "user"},
            "occurred_at": at(seconds),
            "content": blocks or [{"block_id": "b1", "type": "text", "text": text}],
            "mapping_revision": "synthetic-v1",
        }
        item = {
            "fixture_event_id": fid,
            "episode_id": ep,
            "replay_seq": seq,
            "fixture_received_at": at(seconds),
            "auth_subject_ref": subject,
            "payload": payload,
        }
        events.append(item)
        for block in payload["content"]:
            units.append(
                {
                    "unit_id": f"{fid}#{block['block_id']}",
                    "fixture_event_id": fid,
                    "source_revision": revision,
                    "block_id": block["block_id"],
                    "start": 0,
                    "end": len(block["text"]),
                    "quote": block["text"],
                }
            )
        return item

    def question(
        qid,
        ep,
        seq,
        seconds,
        cutoff,
        text,
        groups=(),
        atoms=(),
        expected="answer",
        forbidden=(),
        tags=(),
        subject="alice",
        **kwargs,
    ):
        questions.append(
            {
                "question_id": qid,
                "episode_id": ep,
                "checkpoint_id": f"cp-{qid}",
                "replay_seq": seq,
                "query_at": at(seconds),
                "cutoff_replay_seq": cutoff,
                "knowledge_cutoff_at": at(seconds),
                "target_valid_at": at(seconds),
                "auth_subject_ref": subject,
                "project_id": subjects[subject].project_id,
                "question": text,
                **kwargs,
            }
        )
        labels.append(
            {
                "question_id": qid,
                "expected_action": expected,
                "answer_atoms": [
                    {"atom_id": f"atom-{i}", "accepted_text": [value], "evidence_sets": support}
                    for i, (value, support) in enumerate(atoms)
                ],
                "gold_evidence_sets": groups,
                "forbidden_claims": forbidden,
                "tags": tags,
                "cluster_id": ep,
            }
        )

    event("budget-1", "budget", 10, 0, "项目 A 预算为 100000 CNY。", source="budget")
    question(
        "budget-before",
        "budget",
        11,
        6,
        10,
        "项目 A 预算",
        [["budget-1#b1"]],
        [("100000 CNY", [["budget-1#b1"]])],
        forbidden=["150000", "80000"],
        tags=["prefix"],
    )
    update = event(
        "budget-2",
        "budget",
        20,
        10,
        "项目 A 预算已更新为 150000 CNY。",
        source="budget",
        revision="2",
        source_seq=2,
    )
    question(
        "budget-immediate",
        "budget",
        21,
        10.001,
        20,
        "项目 A 预算",
        [["budget-2#b1"]],
        [("150000 CNY", [["budget-2#b1"]])],
        forbidden=["100000"],
        tags=["update", "freshness"],
    )
    event("budget-b", "budget", 22, 10.002, "项目 A 预算为 80000 CNY。", subject="bob")
    question(
        "budget-isolation",
        "budget",
        23,
        10.003,
        22,
        "项目 A 预算",
        [["budget-2#b1"]],
        [("150000 CNY", [["budget-2#b1"]])],
        forbidden=["80000"],
        tags=["tenant_isolation"],
    )
    events.append(
        {
            **update,
            "fixture_event_id": "budget-retry",
            "replay_seq": 24,
            "fixture_received_at": at(10.004),
            "payload": {**update["payload"], "delivery_id": "budget-retry"},
        }
    )
    question(
        "budget-duplicate",
        "budget",
        25,
        10.005,
        24,
        "项目 A 预算",
        [["budget-2#b1"]],
        [("150000 CNY", [["budget-2#b1"]])],
        tags=["idempotency"],
    )
    deletion = {
        **update["payload"],
        "delivery_id": "budget-delete",
        "source_event_id": "deletion",
        "source_revision": "1",
        "source_sequence": 3,
        "event_type": "message.delete",
        "occurred_at": at(20),
        "content": [],
        "deletion_of": {"source_stream_id": "chat", "source_event_id": "budget", "entity_type": "message"},
    }
    events.append(
        {
            "fixture_event_id": "budget-delete",
            "episode_id": "budget",
            "replay_seq": 30,
            "fixture_received_at": at(20),
            "auth_subject_ref": "alice",
            "payload": deletion,
        }
    )
    question(
        "budget-deleted",
        "budget",
        31,
        20.001,
        30,
        "项目 A 预算",
        expected="insufficient_evidence",
        forbidden=["150000", "100000", "80000"],
        tags=["deletion", "stale_index"],
    )
    question(
        "budget-other-tenant",
        "budget",
        32,
        20.002,
        30,
        "项目 A 预算",
        [["budget-b#b1"]],
        [("80000 CNY", [["budget-b#b1"]])],
        subject="bob",
        tags=["tenant_isolation"],
    )
    question(
        "budget-history-blocked",
        "budget",
        33,
        21,
        10,
        "项目 A 当时预算",
        [["budget-1#b1"]],
        [("100000 CNY", [["budget-1#b1"]])],
        time_mode="as_known_at",
        knowledge_cutoff_at=at(6),
        target_valid_at=at(6),
        tags=["blocked_dependency"],
    )
    question(
        "facts-blocked",
        "budget",
        34,
        22,
        30,
        "项目 A 预算",
        expected="insufficient_evidence",
        required_capabilities=["facts"],
        tags=["blocked_dependency"],
    )
    question(
        "project-denied",
        "budget",
        35,
        23,
        30,
        "预算",
        expected="deny",
        project_id="forbidden",
        tags=["project_isolation"],
    )

    event("stone-1", "languages", 10, 0, "最终选用灰色 A-123 石材。", conversation="design")
    event("stone-2", "languages", 20, 1, "採用する品番は JP-900、色は青です。", conversation="japanese")
    question(
        "chinese-model",
        "languages",
        21,
        2,
        20,
        "A-123",
        [["stone-1#b1"]],
        [("灰色 A-123", [["stone-1#b1"]])],
        tags=["chinese", "exact"],
    )
    question(
        "japanese-model",
        "languages",
        22,
        3,
        20,
        "JP-900",
        [["stone-2#b1"]],
        [("JP-900、色は青", [["stone-2#b1"]])],
        tags=["japanese", "exact"],
    )
    question(
        "conversation-isolation",
        "languages",
        23,
        4,
        20,
        "JP-900",
        expected="insufficient_evidence",
        conversation_id="design",
        tags=["conversation"],
    )
    question(
        "no-evidence",
        "languages",
        24,
        5,
        20,
        "ZQX-999999",
        expected="insufficient_evidence",
        tags=["unanswerable"],
    )

    event("project-location", "combined", 10, 0, "项目 C 位于苏州。")
    event("project-owner", "combined", 20, 1, "项目 C 负责人是李明。")
    question(
        "combined-evidence",
        "combined",
        21,
        2,
        20,
        "项目 C",
        [["project-location#b1", "project-owner#b1"]],
        [("苏州", [["project-location#b1"]]), ("李明", [["project-owner#b1"]])],
        tags=["multi_evidence"],
    )
    event("project-copy", "combined", 30, 3, "项目 C 位于苏州。")
    question(
        "alternative-evidence",
        "combined",
        31,
        4,
        30,
        "苏州",
        [["project-location#b1"], ["project-copy#b1"]],
        [("苏州", [["project-location#b1"], ["project-copy#b1"]])],
        tags=["alternative_evidence"],
    )

    event(
        "blocks",
        "spans",
        10,
        0,
        "",
        blocks=[
            {"block_id": "b1", "type": "text", "text": "验收项目 D 的说明如下。"},
            {"block_id": "b2", "type": "text", "text": "验收口令是 SAPPHIRE-742。"},
        ],
    )
    question(
        "block-content",
        "spans",
        11,
        1,
        10,
        "验收",
        [["blocks#b2"]],
        [("SAPPHIRE-742", [["blocks#b2"]])],
        tags=["content_span", "budget"],
    )
    event("future-canary", "spans", 20, 2, "未来口令是 FUTURE-92817。")
    question(
        "future-canary-visible",
        "spans",
        21,
        3,
        20,
        "FUTURE-92817",
        [["future-canary#b1"]],
        [("FUTURE-92817", [["future-canary#b1"]])],
        tags=["prefix"],
    )
    labels[-2]["forbidden_claims"] = ["FUTURE-92817"]

    # The proposal's business update is a NEW message, not a source-document revision.
    # Keep this known gap in the denominator: today's raw core/quote host returns both values.
    event("business-budget-1", "business_updates", 10, 0, "项目 B 当前预算是 100000 CNY。")
    question(
        "business-before",
        "business_updates",
        11,
        6,
        10,
        "项目 B 预算",
        [["business-budget-1#b1"]],
        [("100000 CNY", [["business-budget-1#b1"]])],
        tags=["business_update"],
    )
    event("business-budget-2", "business_updates", 20, 10, "项目 B 从今天起预算调整为 150000 CNY。")
    question(
        "business-new-message-update",
        "business_updates",
        21,
        10.001,
        20,
        "项目 B 预算",
        [["business-budget-2#b1"]],
        [("150000 CNY", [["business-budget-2#b1"]])],
        forbidden=["100000"],
        tags=["business_update", "state_selection"],
    )

    contents = {
        "public/episodes.jsonl": [
            {"episode_id": ep, "description": "Synthetic local regression"}
            for ep in ("budget", "languages", "combined", "spans", "business_updates")
        ],
        "public/events.jsonl": events,
        "public/questions.jsonl": questions,
        "evaluator_only/evidence_units.jsonl": units,
        "evaluator_only/gold_questions.jsonl": labels,
    }
    hashes = {}
    for name, values in contents.items():
        data = ("".join(canonical(value) + "\n" for value in values)).encode("utf-8")
        (root / name).write_bytes(data)
        hashes[name] = digest(data)
    manifest = DatasetManifest(
        dataset_id="text-foundation-pilot",
        revision="1",
        split="development",
        license="Project-authored synthetic fixtures; no external or personal data",
        synthetic=True,
        annotation_status="synthetic_unreviewed",
        files=hashes,
        subjects=subjects,
    )
    (root / "manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    experiment = Experiment(
        experiment_id="text-foundation-pilot",
        variants=(
            Variant(variant_id="raw_only", index_enabled=False),
            Variant(variant_id="lexical_delayed", worker_delay_ms=5000),
        ),
    )
    (root / "experiment.json").write_text(experiment.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return load_dataset(root)
