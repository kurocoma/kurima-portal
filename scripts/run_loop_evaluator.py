from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from build_eval_package import build_package  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_inline_prompt(package_dir: Path, agent_id: str) -> str:
    task = (package_dir / "task.md").read_text(encoding="utf-8")
    criteria = (package_dir / "criteria.yaml").read_text(encoding="utf-8")
    validation_report = json.loads((package_dir / "validation-report.json").read_text(encoding="utf-8"))
    artifacts = []
    for relative in ("artifacts/generated-ne-to-yamato.csv", "reference/ne-to-yamato2606260043.csv"):
        path = package_dir / relative
        artifacts.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    evidence = {
        "validation_report": validation_report,
        "artifact_metadata": artifacts,
    }
    return (
        "Evaluate this isolated Yamato B2 package using only the inline package content below. "
        "Do not call shell, browser, filesystem, network, or other tools; the sandbox may not allow file reads. "
        "Do not read outside this package. Do not use plan, generator report, state, prior eval, previous score, or best score. "
        f"Set evaluator_agent_id exactly to {agent_id}. "
        "Return only JSON matching eval-schema.json. Do not mention previous scores.\n\n"
        "## task.md\n"
        f"{task}\n\n"
        "## criteria.yaml\n"
        f"{criteria}\n\n"
        "## validation-report.json and artifact metadata\n"
        f"{json.dumps(evidence, ensure_ascii=False, indent=2)}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--hard-gate-report", type=Path, required=True)
    parser.add_argument("--output-eval", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    package_dir = ROOT / ".loop" / "current" / "eval-packages" / f"turn-{args.turn:03d}-{stamp}"
    manifest = build_package(turn=args.turn, hard_gate_report=args.hard_gate_report, output_dir=package_dir)
    output_eval = args.output_eval or ROOT / ".loop" / "current" / "turns" / f"turn-{args.turn:03d}-eval.json"
    context_path = ROOT / ".loop" / "current" / "turns" / f"turn-{args.turn:03d}-evaluator-context.json"

    agent_id = f"codex-exec-evaluator-{stamp}"
    context = {
        "agent_id": agent_id,
        "agent_type": "evaluator",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "input_files": manifest["input_files"],
        "forbidden_files": manifest["forbidden_files"],
        "context_strategy": "isolated-evaluation-package-inline-evidence",
        "previous_thread_reused": False,
        "sandbox_mode": "read-only",
        "package_dir": str(package_dir),
        "output_eval": str(output_eval),
        "finished_at": None,
    }

    if args.dry_run:
        context["finished_at"] = datetime.now().isoformat(timespec="seconds")
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"dry_run": True, "package_dir": str(package_dir), "context": str(context_path)}, ensure_ascii=False))
        return 0

    prompt = build_inline_prompt(package_dir, agent_id)
    env = os.environ.copy()
    env["EVAL_LOOP_ROLE"] = "evaluator"
    completed = subprocess.run(
        [
            "codex",
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "-C",
            str(package_dir),
            "--output-schema",
            "eval-schema.json",
            "-o",
            str(output_eval),
            prompt,
        ],
        cwd=package_dir,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=600,
    )
    context["finished_at"] = datetime.now().isoformat(timespec="seconds")
    context["returncode"] = completed.returncode
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    if completed.returncode != 0:
        print(completed.stdout, file=sys.stderr)
        print(completed.stderr, file=sys.stderr)
        return completed.returncode
    print(json.dumps({"eval_file": str(output_eval), "context": str(context_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
