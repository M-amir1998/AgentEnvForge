"""Regenerate schemas/task-v1.schema.json from the pydantic model.

Run after changing agentenvforge.tasks.schema:  uv run python scripts/generate_schema.py
A unit test fails if the committed schema is out of date.
"""

import json
from pathlib import Path

from agentenvforge.tasks.schema import TaskSpec

OUTPUT = Path(__file__).resolve().parents[1] / "schemas" / "task-v1.schema.json"

if __name__ == "__main__":
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **TaskSpec.model_json_schema(),
    }
    OUTPUT.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT}")
