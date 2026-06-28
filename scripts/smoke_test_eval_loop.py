from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
STATE = ROOT / ".loop" / "current" / "state.json"


def breakdown_keys() -> tuple[str, ...]:
    schema = json.loads((ROOT / ".loop" / "current" / "eval-schema.json").read_text(encoding="utf-8"))
    return tuple(schema["properties"]["quality"]["properties"]["breakdown"]["required"])


def fail(message: str) -> int:
    print(f"smoke failed: {message}", file=sys.stderr)
    return 1


def run_cmd(args: list[object], expect: int = 0) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(item) for item in args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if completed.returncode != expect:
        raise RuntimeError(
            f"expected={expect} actual={completed.returncode}: {' '.join(map(str, args))}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def write_eval(path: Path, score: int = 90, evaluator_id: str | None = None) -> None:
    payload = {
        "score": score,
        "quality": {"overall": score, "breakdown": {key: score for key in breakdown_keys()}},
        "hard_gate_findings": [],
        "evidence": [{"kind": "report", "path": "validation/runs/smoke/report.json", "summary": "smoke evidence"}],
        "feedback": "固定criteriaに基づく評価です。具体的な追加修正はありません。",
        "passed": score >= 90,
        "evaluator_agent_id": evaluator_id or f"smoke-{uuid.uuid4()}",
        "evaluated_artifacts": ["portal_app/services/yamato_conversion.py"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    try:
        if tomllib is None:
            return fail("tomllib unavailable")

        with (ROOT / ".codex" / "agents" / "loop-generator.toml").open("rb") as handle:
            generator = tomllib.load(handle)
        with (ROOT / ".codex" / "agents" / "loop-evaluator.toml").open("rb") as handle:
            evaluator = tomllib.load(handle)
        assert generator["role"] == "generator", "generator role mismatch"
        assert evaluator["role"] == "evaluator", "evaluator role mismatch"
        assert generator["sandbox_mode"] == "workspace-write", "generator sandbox mismatch"
        assert evaluator["sandbox_mode"] == "read-only", "evaluator sandbox mismatch"
        assert "run_loop_generator.py" in generator["execution"], "generator runner missing"
        assert "run_loop_evaluator.py" in evaluator["execution"], "evaluator runner missing"
        assert generator["environment"]["EVAL_LOOP_ROLE"] == "generator", "generator role environment missing"
        assert evaluator["environment"]["EVAL_LOOP_ROLE"] == "evaluator", "evaluator role environment missing"
        assert "state.json" in " ".join(generator["forbidden_inputs"]), "generator state forbidden missing"
        assert "state.json" in " ".join(evaluator["forbidden_inputs"]), "evaluator state forbidden missing"

        help_text = run_cmd(["codex", "exec", "--help"]).stdout
        assert "--ephemeral" in help_text and "--sandbox" in help_text and "--output-schema" in help_text, "codex exec options missing"

        with tempfile.TemporaryDirectory(prefix="eval-loop-smoke-") as raw_tmp:
            tmp = Path(raw_tmp)
            eval_file = tmp / "eval.json"
            write_eval(eval_file)
            run_cmd([PYTHON, "scripts/validate_eval_schema.py", eval_file])

            invalid_eval = tmp / "invalid-eval.json"
            invalid_eval.write_text(json.dumps({"score": 90}), encoding="utf-8")
            run_cmd([PYTHON, "scripts/validate_eval_schema.py", invalid_eval], expect=1)

            hard_gate = json.loads(
                run_cmd(
                    [
                        PYTHON,
                        "scripts/validate_equivalence.py",
                        "--config",
                        "validation/equivalence.yaml",
                        "--output-dir",
                        tmp / "equivalence",
                    ]
                ).stdout
            )
            assert hard_gate["passed"] is True, "hard gate did not pass"

            generator_dry = json.loads(run_cmd([PYTHON, "scripts/run_loop_generator.py", "--turn", "0", "--dry-run"]).stdout)
            generator_context = json.loads(Path(generator_dry["context"]).read_text(encoding="utf-8"))
            assert generator_context["previous_thread_reused"] is False, "generator context reuse flag invalid"
            assert generator_context["context_strategy"] == "sanitized-generator-workspace", "generator isolation strategy invalid"
            generator_workspace = Path(generator_dry["workspace"])
            assert not (generator_workspace / ".loop" / "current" / "state.json").exists(), "generator workspace includes state"
            assert not (generator_workspace / ".loop" / "current" / "feedback.md").exists(), "generator workspace includes feedback"
            assert not (generator_workspace / ".loop" / "current" / "turns" / "turn-000-eval.json").exists(), "generator workspace includes eval"

            evaluator_dry = json.loads(
                run_cmd(
                    [
                        PYTHON,
                        "scripts/run_loop_evaluator.py",
                        "--turn",
                        "0",
                        "--hard-gate-report",
                        hard_gate["report_file"],
                        "--dry-run",
                    ]
                ).stdout
            )
            evaluator_context = json.loads(Path(evaluator_dry["context"]).read_text(encoding="utf-8"))
            assert evaluator_context["previous_thread_reused"] is False, "evaluator context reuse flag invalid"
            assert evaluator_context["context_strategy"] == "isolated-evaluation-package-inline-evidence", "evaluator isolation strategy invalid"

            package_dir = tmp / "package"
            run_cmd(
                [
                    PYTHON,
                    "scripts/build_eval_package.py",
                    "--turn",
                    "0",
                    "--hard-gate-report",
                    hard_gate["report_file"],
                    "--output-dir",
                    package_dir,
                ]
            )
            manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
            joined_inputs = " ".join(manifest["input_files"])
            assert "state.json" not in joined_inputs, "eval package includes state"
            assert "plan" not in joined_inputs, "eval package includes plan"
            assert "report.md" not in joined_inputs, "eval package includes report"

            state_file = tmp / "state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "active": True,
                        "status": "running",
                        "iteration": 0,
                        "score": 0,
                        "threshold": 90,
                        "passed": False,
                        "hard_gates_passed": False,
                        "consecutive_passes": 0,
                        "required_consecutive_passes": 5,
                        "best_score": 0,
                        "best_iteration": None,
                        "best_ref": None,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            plan = tmp / "plan.md"
            report = tmp / "report.md"
            plan.write_text("plan", encoding="utf-8")
            report.write_text("report", encoding="utf-8")
            run_cmd(
                [
                    PYTHON,
                    "scripts/record_eval.py",
                    eval_file,
                    "--state-file",
                    state_file,
                    "--hard-gate-report",
                    hard_gate["report_file"],
                    "--plan-file",
                    plan,
                    "--report-file",
                    report,
                ]
            )
            state = json.loads(state_file.read_text(encoding="utf-8"))
            assert state["consecutive_passes"] == 1, "consecutive pass count mismatch"
            assert state["awaiting_orchestrator_turn"] is True, "record_eval did not stop after one turn"

            run_cmd(
                [
                    PYTHON,
                    "scripts/record_eval.py",
                    eval_file,
                    "--state-file",
                    state_file,
                    "--hard-gate-report",
                    hard_gate["report_file"],
                    "--plan-file",
                    plan,
                    "--report-file",
                    report,
                ],
                expect=1,
            )

            forged_dir = tmp / "forged"
            forged_dir.mkdir()
            forged_report_path = forged_dir / "report.json"
            forged_csv = forged_dir / "generated-ne-to-yamato.csv"
            forged_csv.write_text("bad\r\n", encoding="cp932")
            forged = dict(hard_gate)
            forged_hashes = dict(hard_gate["hashes"])
            forged.update(
                {
                    "run_id": f"forged-{uuid.uuid4()}",
                    "generated_csv": str(forged_csv),
                    "report_file": str(forged_report_path),
                    "passed": True,
                    "differences": [],
                    "hashes": forged_hashes,
                }
            )
            forged_report_path.write_text(json.dumps(forged, ensure_ascii=False, indent=2), encoding="utf-8")
            forged_state = tmp / "forged-state.json"
            forged_state.write_text(
                json.dumps(
                    {
                        "active": True,
                        "status": "running",
                        "iteration": 0,
                        "score": 0,
                        "threshold": 90,
                        "passed": False,
                        "hard_gates_passed": False,
                        "consecutive_passes": 0,
                        "required_consecutive_passes": 5,
                        "best_score": 0,
                        "best_iteration": None,
                        "best_ref": None,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            forged_eval = tmp / "forged-eval.json"
            write_eval(forged_eval, evaluator_id=f"smoke-forged-{uuid.uuid4()}")
            run_cmd(
                [
                    PYTHON,
                    "scripts/record_eval.py",
                    forged_eval,
                    "--state-file",
                    forged_state,
                    "--hard-gate-report",
                    forged_report_path,
                    "--plan-file",
                    plan,
                    "--report-file",
                    report,
                ],
                expect=1,
            )

            reset_state = tmp / "reset-state.json"
            reset_payload = {
                "active": True,
                "status": "running",
                "iteration": 1,
                "score": 90,
                "threshold": 90,
                "passed": True,
                "hard_gates_passed": True,
                "consecutive_passes": 4,
                "required_consecutive_passes": 5,
                "best_score": 90,
                "best_iteration": 0,
                "best_ref": None,
            }
            for key, value in hard_gate["hashes"].items():
                if isinstance(value, str):
                    reset_payload[key] = value
            reset_payload["implementation_hash"] = "changed-implementation-hash"
            reset_state.write_text(json.dumps(reset_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            reset_eval = tmp / "reset-eval.json"
            write_eval(reset_eval, evaluator_id=f"smoke-reset-{uuid.uuid4()}")
            run_cmd(
                [
                    PYTHON,
                    "scripts/record_eval.py",
                    reset_eval,
                    "--state-file",
                    reset_state,
                    "--hard-gate-report",
                    hard_gate["report_file"],
                    "--plan-file",
                    plan,
                    "--report-file",
                    report,
                ]
            )
            reset_after = json.loads(reset_state.read_text(encoding="utf-8"))
            assert reset_after["consecutive_passes"] == 1, "hash change did not reset consecutive passes"

            missing_hash_state = tmp / "missing-hash-state.json"
            missing_hash_state.write_text(
                json.dumps(
                    {
                        "active": True,
                        "status": "running",
                        "iteration": 2,
                        "score": 90,
                        "threshold": 90,
                        "passed": True,
                        "hard_gates_passed": True,
                        "consecutive_passes": 4,
                        "required_consecutive_passes": 5,
                        "best_score": 90,
                        "best_iteration": 1,
                        "best_ref": None,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            missing_hash_eval = tmp / "missing-hash-eval.json"
            write_eval(missing_hash_eval, evaluator_id=f"smoke-missing-hash-{uuid.uuid4()}")
            run_cmd(
                [
                    PYTHON,
                    "scripts/record_eval.py",
                    missing_hash_eval,
                    "--state-file",
                    missing_hash_state,
                    "--hard-gate-report",
                    hard_gate["report_file"],
                    "--plan-file",
                    plan,
                    "--report-file",
                    report,
                ]
            )
            missing_after = json.loads(missing_hash_state.read_text(encoding="utf-8"))
            assert missing_after["consecutive_passes"] == 1, "missing hash did not reset consecutive passes"

        backup = STATE.read_text(encoding="utf-8")
        marker = ROOT / ".loop" / "current" / ".state_parse_error_blocked"
        try:
            STATE.write_text(
                json.dumps(
                    {
                        "active": True,
                        "status": "running",
                        "iteration": 99,
                        "max_iterations": 100,
                        "score": 89,
                        "threshold": 90,
                        "passed": False,
                        "hard_gates_passed": True,
                        "consecutive_passes": 0,
                        "required_consecutive_passes": 5,
                        "last_stop_blocked_iteration": None,
                        "awaiting_orchestrator_turn": False,
                        "blocked_reason": None,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            before_first = STATE.read_text(encoding="utf-8")
            first = run_cmd([PYTHON, ".codex/hooks/stop_eval_loop.py"]).stdout
            assert '"decision": "block"' in first, "89 score did not block"
            after_first = STATE.read_text(encoding="utf-8")
            assert after_first == before_first, "stop hook mutated state on first block"
            second = run_cmd([PYTHON, ".codex/hooks/stop_eval_loop.py"]).stdout
            assert '"decision": "block"' in second, "duplicate incomplete iteration did not stay blocked"
            assert STATE.read_text(encoding="utf-8") == after_first, "duplicate block mutated state"

            state = json.loads(after_first)
            state["awaiting_orchestrator_turn"] = True
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            awaiting = run_cmd([PYTHON, ".codex/hooks/stop_eval_loop.py"]).stdout
            assert '"decision": "allow"' in awaiting, "awaiting orchestrator turn did not allow stop"

            if marker.exists():
                marker.unlink()
            STATE.write_text("{", encoding="utf-8")
            broken = run_cmd([PYTHON, ".codex/hooks/stop_eval_loop.py"]).stdout
            assert '"decision": "block"' in broken, "broken state did not block"
        finally:
            STATE.write_text(backup, encoding="utf-8")
            if marker.exists():
                marker.unlink()

        run_cmd([PYTHON, ".codex/hooks/pre_compact_checkpoint.py"])
        run_cmd([PYTHON, ".codex/hooks/session_context.py"])

    except Exception as exc:
        return fail(str(exc))

    print("smoke ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
