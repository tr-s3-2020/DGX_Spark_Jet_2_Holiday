"""Export current task2 contracts; old schemas remain legacy-only."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from implicit_health_triage.task2.schemas import (  # noqa: E402
    HealthSignal,
    IgnoredOutput,
    TriageInput,
    TriageOutput,
)


def build():
    return {
        ROOT / "schemas" / "task2" / f"{model.__name__}.schema.json": json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                **model.model_json_schema(),
                "x-source": f"{model.__module__}.{model.__name__}",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
        for model in (TriageInput, TriageOutput, HealthSignal, IgnoredOutput)
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path, text in build().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                raise SystemExit(f"Out of date: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
