from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from validate_eval_schema import validate_eval_payload  # noqa: E402


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object JSON: {path}")
    return value


def fail(message: str) -> int:
    print(f"completion check failed: {message}", file=sys.stderr)
    return 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_files(paths: list[Path]) -> str:
    return sha256_text(":".join(sha256_file(path) for path in paths))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", type=Path, default=Path(".loop/current/state.json"))
    args = parser.parse_args(argv)

    try:
        state = load_json(args.state_file)
        if state.get("status") != "complete" or state.get("active", True):
            return fail("state is not complete/inactive")
        if not state.get("hard_gates_passed") or not state.get("passed"):
            return fail("state pass flags are not complete")
        if state.get("score", 0) < state.get("threshold", 90):
            return fail("score below threshold")
        if state.get("consecutive_passes", 0) < state.get("required_consecutive_passes", 5):
            return fail("not enough consecutive passes")

        eval_file = Path(str(state.get("last_eval_file", "")))
        hard_gate_file = Path(str(state.get("last_validation_report", "")))
        best_ref = Path(str(state.get("best_ref", "")))
        if not eval_file.exists():
            return fail(f"last eval missing: {eval_file}")
        if not hard_gate_file.exists():
            return fail(f"hard gate report missing: {hard_gate_file}")
        if not best_ref.exists():
            return fail(f"best ref missing: {best_ref}")

        schema = load_json(ROOT / ".loop" / "current" / "eval-schema.json")
        evaluation = load_json(eval_file)
        validate_eval_payload(evaluation, schema)
        report = load_json(hard_gate_file)
        if report.get("name") != "yamato_b2_csv_equivalence" or report.get("passed") is not True:
            return fail("hard gate report is not a passed Yamato B2 equivalence report")
        generated_csv = Path(str(report.get("generated_csv", "")))
        reference_csv = Path(str(report.get("reference_csv", "")))
        source_csv = Path(str(report.get("source_csv", "")))
        master_book = Path(str(report.get("master_book", "")))
        for path in (generated_csv, reference_csv, source_csv, master_book):
            if not path.exists():
                return fail(f"hard gate dependency missing: {path}")
        if generated_csv.read_bytes() != reference_csv.read_bytes():
            return fail("generated CSV does not byte-match reference CSV")
        report_hashes = report.get("hashes", {})
        recomputed = {
            "source_csv": sha256_file(source_csv),
            "master_book": sha256_file(master_book),
            "reference_csv": sha256_file(reference_csv),
            "generated_csv": sha256_file(generated_csv),
            "implementation_hash": sha256_files(
                [
                    ROOT / "portal_app" / "services" / "yamato_conversion.py",
                    ROOT / "portal_app" / "services" / "paths.py",
                ]
            ),
            "validator_hash": sha256_text(
                (ROOT / "scripts" / "validate_equivalence.py").read_text(encoding="utf-8-sig")
                + (ROOT / "validation" / "equivalence.yaml").read_text(encoding="utf-8-sig")
            ),
            "criteria_hash": sha256_file(ROOT / ".loop" / "current" / "criteria.yaml"),
            "task_hash": sha256_file(ROOT / ".loop" / "current" / "task.md"),
            "eval_schema_hash": sha256_file(ROOT / ".loop" / "current" / "eval-schema.json"),
        }
        recomputed["source_hash"] = sha256_text(f"{recomputed['source_csv']}:{recomputed['master_book']}")
        recomputed["baseline_hash"] = recomputed["reference_csv"]
        recomputed["generated_hash"] = recomputed["generated_csv"]
        for key, value in recomputed.items():
            if report_hashes.get(key) != value:
                return fail(f"hard gate hash mismatch: {key}")
        for key in (
            "source_hash",
            "baseline_hash",
            "implementation_hash",
            "validator_hash",
            "criteria_hash",
            "task_hash",
            "eval_schema_hash",
        ):
            if report_hashes.get(key) != state.get(key):
                return fail(f"state hash mismatch: {key}")
    except Exception as exc:
        return fail(str(exc))

    print("goal completion ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
