#!/usr/bin/env python3
"""Export OpenAPI schema to docs/openapi.json for sharing or importing into Postman."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import app  # noqa: E402

OUTPUT = ROOT / "docs" / "openapi.json"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    schema = app.openapi()
    OUTPUT.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
