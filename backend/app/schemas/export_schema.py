"""Write the unified report JSON Schema to docs/report.schema.json.

Run from the repo root:  python -m backend.app.schemas.export_schema
or from backend/:        python -m app.schemas.export_schema
"""
from __future__ import annotations

import json
from pathlib import Path

from .report import UnifiedReport

OUT = Path(__file__).resolve().parents[3] / "docs" / "report.schema.json"


def build_schema() -> dict:
    schema = UnifiedReport.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "UnifiedReport"
    return schema


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build_schema(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
