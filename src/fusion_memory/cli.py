import argparse
import json
from pathlib import Path

import uvicorn

from fusion_memory.app import create_app
from fusion_memory.config import load_settings


def main():
    parser = argparse.ArgumentParser(description="Fusion Memory local text foundation")
    parser.add_argument("command", choices=["serve", "init", "export-schema"])
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, default=Path("contracts/openapi.json"))
    args = parser.parse_args()
    settings = load_settings(args.data_root, args.config)
    if args.command == "init":
        print(f"Configuration: {args.config or settings.data_root / 'config' / 'local.json'}")
        print("Credential stored in that file; not printed. Read it locally for authorized API calls.")
    elif args.command == "export-schema":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(create_app(settings).openapi(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Schema: {args.output}")
    else:
        uvicorn.run(create_app(settings), host="127.0.0.1", port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
