import json
import os
import secrets
from pathlib import Path

from platformdirs import user_data_path
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from fusion_memory.contracts import Scope


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=24)
    scope: Scope


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_root: Path
    credentials: list[Credential] = Field(min_length=1)
    index_enabled: bool = True
    index_interval_ms: int = Field(default=250, ge=20, le=60_000)


def load_settings(root: Path | None = None, config: Path | None = None) -> Settings:
    root = root or Path(
        os.environ.get("FUSION_MEMORY_HOME", user_data_path("fusion-memory", appauthor=False))
    )
    if not root.is_absolute():
        root = root.absolute()
    if str(root).startswith("\\\\"):
        raise ValueError("Network-share data directories are not supported")
    root.mkdir(parents=True, exist_ok=True)
    path = config or root / "config" / "local.json"
    if not path.exists():
        if config:
            raise ValueError("Explicit config file does not exist")
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            data_root=root,
            credentials=[
                Credential(
                    token=secrets.token_urlsafe(32),
                    scope=Scope(
                        tenant_id="local",
                        project_id="default",
                        connector_id="local-http",
                        middleware_id="http",
                        origin_namespace="local:default",
                        actor_ids=("local-user",),
                        permissions=("read", "write", "checkpoint"),
                    ),
                )
            ],
        )
        data = settings.model_dump(mode="json")
        data["credentials"][0]["token"] = settings.credentials[0].token.get_secret_value()
        # Exclusive create: never replace an existing identity on concurrent startup.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
    settings = Settings.model_validate_json(path.read_text(encoding="utf-8"))
    if settings.data_root.resolve() != root.resolve():
        raise ValueError("Config data_root differs: pass the matching --data-root; migration is not implicit")
    tokens = [c.token.get_secret_value() for c in settings.credentials]
    if len(set(tokens)) != len(tokens):
        raise ValueError("Each credential must bind exactly one scope")
    return settings
