from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from validate_eval_schema import validate_eval_payload  # noqa: E402


HASH_KEYS = (
    "source_hash",
    "baseline_hash",
    "implementation_hash",
    "validator_hash",
    "criteria_hash",
    "task_hash",
    "eval_schema_hash",
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected object JSON: {path}")
    return value


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


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


def copy_best_artifact(loop_root: Path, iteration: int, hard_gate_report: dict[str, Any], eval_file: Path) -> str:
    snapshots = loop_root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    source = Path(str(hard_gate_report["generated_csv"]))
    if source.exists():
        target = snapshots / f"best-turn-{iteration:03d}-{source.name}"
        shutil.copy2(source, target)
        return str(target)
    target = snapshots / f"best-turn-{iteration:03d}-eval.json"
    shutil.copy2(eval_file, target)
    return str(target)


def validate_hard_gate_report(report: dict[str, Any], report_path: Path) -> None:
    require(report.get("kind") == "yamato_b2_csv_equivalence", "hard gate report kind mismatch")
    require(report.get("passed") is True, "hard gate report is not passed")
    require(isinstance(report.get("run_id"), str) and report["run_id"], "hard gate report run_id missing")
    require(Path(str(report.get("report_file", ""))).resolve() == report_path.resolve(), "hard gate report_file mismatch")
    require(report.get("differences") == [], "hard gate report differences must be empty")
    generated_csv = Path(str(report.get("generated_csv", "")))
    reference_csv = Path(str(report.get("reference_csv", "")))
    source_csv = Path(str(report.get("source_csv", "")))
    master_book = Path(str(report.get("master_book", "")))
    require(generated_csv.exists(), f"generated_csv missing: {generated_csv}")
    require(reference_csv.exists(), f"reference_csv missing: {reference_csv}")
    require(source_csv.exists(), f"source_csv missing: {source_csv}")
    require(master_book.exists(), f"master_book missing: {master_book}")
    require(generated_csv.read_bytes() == reference_csv.read_bytes(), "generated CSV bytes must match reference CSV bytes")
    hashes = report.get("hashes")
    require(isinstance(hashes, dict), "hard gate report hashes missing")
    for key in HASH_KEYS:
        require(isinstance(hashes.get(key), str) and hashes[key], f"hard gate hash missing: {key}")
    require(hashes.get("source_csv") == sha256_file(source_csv), "source_csv hash mismatch")
    require(hashes.get("master_book") == sha256_file(master_book), "master_book hash mismatch")
    require(hashes.get("source_hash") == sha256_text(f"{hashes['source_csv']}:{hashes['master_book']}"), "source_hash mismatch")
    require(hashes.get("reference_csv") == sha256_file(reference_csv), "reference hash mismatch")
    require(hashes.get("generated_csv") == sha256_file(generated_csv), "generated hash mismatch")
    require(
        hashes.get("implementation_hash")
        == sha256_files(
            [
                ROOT / "portal_app" / "services" / "yamato_conversion.py",
                ROOT / "portal_app" / "services" / "paths.py",
            ]
        ),
        "implementation hash mismatch",
    )
    require(
        hashes.get("validator_hash")
        == sha256_text(
            (ROOT / "scripts" / "validate_equivalence.py").read_text(encoding="utf-8-sig")
            + (ROOT / "validation" / "equivalence.yaml").read_text(encoding="utf-8-sig")
        ),
        "validator hash mismatch",
    )
    require(hashes.get("criteria_hash") == sha256_file(ROOT / ".loop" / "current" / "criteria.yaml"), "criteria hash mismatch")
    require(hashes.get("task_hash") == sha256_file(ROOT / ".loop" / "current" / "task.md"), "task hash mismatch")
    require(hashes.get("eval_schema_hash") == sha256_file(ROOT / ".loop" / "current" / "eval-schema.json"), "eval schema hash mismatch")
    require(hashes.get("generated_hash") == hashes.get("baseline_hash"), "generated hash must match baseline hash")


def rerun_hard_gate() -> tuple[dict[str, Any], Path]:
    run_dir = ROOT / "validation" / "runs" / f"recorded-yamato-b2-equivalence-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "validate_equivalence.py"),
            "--config",
            str(ROOT / "validation" / "equivalence.yaml"),
            "--output-dir",
            str(run_dir),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise ValueError(f"hard gate rerun failed: {completed.stderr or completed.stdout}")
    report = json.loads(completed.stdout)
    report_path = Path(str(report["report_file"]))
    validate_hard_gate_report(report, report_path)
    return report, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("eval_file", type=Path)
    parser.add_argument("--state-file", type=Path, default=Path(".loop/current/state.json"))
    parser.add_argument("--hard-gate-report", type=Path, required=True)
    parser.add_argument("--plan-file", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        state = load_json(args.state_file)
        evaluation = load_json(args.eval_file)
        schema = load_json(ROOT / ".loop" / "current" / "eval-schema.json")
        validate_eval_payload(evaluation, schema)
        supplied_hard_gate_report = load_json(args.hard_gate_report)
        validate_hard_gate_report(supplied_hard_gate_report, args.hard_gate_report)
        hard_gate_report, hard_gate_report_path = rerun_hard_gate()

        require(str(args.eval_file) != state.get("last_eval_file"), "eval file replay rejected")
        require(hard_gate_report["run_id"] != state.get("last_validation_run_id"), "validation run replay rejected")
        require(evaluation["evaluator_agent_id"] != state.get("last_evaluator_agent_id"), "evaluator id replay rejected")
        require(args.plan_file.exists(), f"plan file missing: {args.plan_file}")
        require(args.report_file.exists(), f"report file missing: {args.report_file}")

        iteration = int(state.get("iteration", 0))
        threshold = float(state.get("threshold", 90))
        score = float(evaluation["score"])
        hard_gates_passed = True
        passed = bool(evaluation.get("passed")) and hard_gates_passed and score >= threshold

        hashes = hard_gate_report["hashes"]
        previous_consecutive = int(state.get("consecutive_passes", 0))
        changed_hash = False
        for key in HASH_KEYS:
            previous = state.get(key)
            if previous_consecutive > 0 and not previous:
                changed_hash = True
            elif previous and previous != hashes[key]:
                changed_hash = True
            state[key] = hashes[key]

        if changed_hash:
            previous_consecutive = 0

        state["score"] = score
        state["hard_gates_passed"] = hard_gates_passed
        state["passed"] = passed
        state["consecutive_passes"] = previous_consecutive + 1 if passed else 0
        state["last_eval_file"] = str(args.eval_file)
        state["last_plan_file"] = str(args.plan_file)
        state["last_report_file"] = str(args.report_file)
        state["last_hard_gate_report"] = str(hard_gate_report_path)
        state["last_validation_report"] = str(hard_gate_report_path)
        state["last_validation_run_id"] = hard_gate_report["run_id"]
        state["last_evaluator_agent_id"] = evaluation["evaluator_agent_id"]
        state["last_stop_blocked_iteration"] = None
        state["awaiting_orchestrator_turn"] = True

        if score > float(state.get("best_score", 0)):
            state["best_score"] = score
            state["best_iteration"] = iteration
            state["best_ref"] = copy_best_artifact(args.state_file.parent, iteration, hard_gate_report, args.eval_file)

        if (
            state["hard_gates_passed"]
            and state["passed"]
            and state["consecutive_passes"] >= int(state.get("required_consecutive_passes", 5))
        ):
            state["status"] = "complete"
            state["active"] = False
            state["awaiting_orchestrator_turn"] = False
        else:
            state["status"] = "running"
            state["active"] = True

        state["iteration"] = iteration + 1
        write_json(args.state_file, state)

        feedback_path = args.state_file.parent / "feedback.md"
        feedback_path.write_text(
            "# Feedback\n\n"
            f"Last eval: {args.eval_file}\n\n"
            f"Score: {score}\n\n"
            f"Passed: {passed}\n\n"
            f"Hard gates passed: {hard_gates_passed}\n\n"
            f"{evaluation['feedback'].strip()}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"record eval failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({"recorded": True, "passed": passed, "score": score}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
