from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA = ROOT / ".loop" / "current" / "eval-schema.json"


class ValidationError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def schema_breakdown_keys(schema: dict[str, Any]) -> tuple[str, ...]:
    try:
        required = schema["properties"]["quality"]["properties"]["breakdown"]["required"]
    except KeyError as exc:
        raise ValidationError(f"schema does not define quality.breakdown.required: {exc}") from exc
    require(isinstance(required, list) and all(isinstance(item, str) for item in required), "schema breakdown keys invalid")
    return tuple(required)


def schema_top_keys(schema: dict[str, Any]) -> tuple[str, ...]:
    required = schema.get("required")
    require(isinstance(required, list) and all(isinstance(item, str) for item in required), "schema required keys invalid")
    return tuple(required)


def require_number(value: Any, name: str) -> float:
    require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{name} must be a number")
    numeric = float(value)
    require(0 <= numeric <= 100, f"{name} must be between 0 and 100")
    return numeric


def validate_eval_payload(payload: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
    schema = schema or load_json(DEFAULT_SCHEMA)
    top_keys = schema_top_keys(schema)
    allowed_top = set(schema.get("properties", {}).keys())
    require(set(payload.keys()) == set(top_keys), f"top-level keys must exactly match schema required keys: {top_keys}")
    extra = set(payload.keys()) - allowed_top
    require(not extra, f"unexpected top-level keys: {sorted(extra)}")

    score = require_number(payload["score"], "score")
    quality = payload["quality"]
    require(isinstance(quality, dict), "quality must be an object")
    require(set(quality.keys()) == {"overall", "breakdown"}, "quality keys must be exactly overall and breakdown")
    overall = require_number(quality["overall"], "quality.overall")
    require(score == overall, "score and quality.overall must match exactly")

    breakdown = quality["breakdown"]
    require(isinstance(breakdown, dict), "quality.breakdown must be an object")
    breakdown_keys = schema_breakdown_keys(schema)
    require(tuple(breakdown.keys()) == breakdown_keys, f"quality.breakdown keys/order must be {breakdown_keys}")
    for key in breakdown_keys:
        require_number(breakdown[key], f"quality.breakdown.{key}")

    require(isinstance(payload["hard_gate_findings"], list), "hard_gate_findings must be an array")
    require(all(isinstance(item, str) for item in payload["hard_gate_findings"]), "hard_gate_findings items must be strings")
    require(isinstance(payload["evidence"], list), "evidence must be an array")
    for index, item in enumerate(payload["evidence"]):
        require(isinstance(item, dict), f"evidence[{index}] must be an object")
        for key in ("kind", "path", "summary"):
            require(isinstance(item.get(key), str) and item[key].strip(), f"evidence[{index}].{key} is required")
    require(isinstance(payload["feedback"], str) and payload["feedback"].strip(), "feedback is required")
    require(isinstance(payload["passed"], bool), "passed must be boolean")
    require(isinstance(payload["evaluator_agent_id"], str) and payload["evaluator_agent_id"].strip(), "evaluator_agent_id is required")
    require(isinstance(payload["evaluated_artifacts"], list), "evaluated_artifacts must be an array")
    require(all(isinstance(item, str) and item.strip() for item in payload["evaluated_artifacts"]), "evaluated_artifacts items must be non-empty strings")

    forbidden_phrases = (
        "previous score",
        "previous eval",
        "best score",
        "last score",
        "state score",
        "前回score",
        "前回のscore",
        "前回より",
        "改善した",
    )
    feedback_lower = payload["feedback"].lower()
    for phrase in forbidden_phrases:
        require(phrase.lower() not in feedback_lower, f"feedback must not compare with previous score: {phrase}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("eval_file", type=Path)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    args = parser.parse_args(argv)

    try:
        payload = load_json(args.eval_file)
        schema = load_json(args.schema)
        require(isinstance(payload, dict), "eval payload must be an object")
        require(isinstance(schema, dict), "schema must be an object")
        validate_eval_payload(payload, schema)
    except Exception as exc:
        print(f"eval schema invalid: {exc}", file=sys.stderr)
        return 1

    print("eval schema ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
