"""Export implemented contracts without creating a real identity or data directory."""

import json
from pathlib import Path

from fusion_memory.app import create_app
from fusion_memory.config import Credential, Settings
from fusion_memory.contracts import (
    Change,
    CheckpointWrite,
    IndexManifest,
    MemoryEvent,
    Receipt,
    Scope,
    SearchRequest,
)

root = Path(__file__).resolve().parents[1]
out = root / "contracts"
out.mkdir(exist_ok=True)
for name, model in {
    "memory-event-1.1": MemoryEvent,
    "receipt-1.2": Receipt,
    "memory-change-1.0": Change,
    "search-request-1.0": SearchRequest,
    "checkpoint-1.0": CheckpointWrite,
    "index-manifest-1.0": IndexManifest,
}.items():
    (out / (name + ".schema.json")).write_text(
        json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
settings = Settings(
    data_root=root / ".local-data",
    credentials=[
        Credential(
            token="schema-only-never-a-live-key",
            scope=Scope(
                tenant_id="schema",
                project_id="schema",
                connector_id="schema",
                middleware_id="schema",
                origin_namespace="schema",
                actor_ids=(),
            ),
        )
    ],
)
(out / "openapi.json").write_text(
    json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2), encoding="utf-8"
)
print("Exported 6 DTO schemas and OpenAPI")
