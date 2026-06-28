from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT = ROOT / ".loop" / "current"

ALLOWED_PATHS = [
    CURRENT / "task.md",
    CURRENT / "criteria.yaml",
    CURRENT / "project-memory.md",
    ROOT / "portal_app" / "services" / "yamato_conversion.py",
    ROOT / "portal_app" / "services" / "paths.py",
    ROOT / "portal_app" / "cli.py",
    ROOT / "portal_app" / "main.py",
    ROOT / "portal_app" / "templates" / "yamato.html",
    ROOT / "validation" / "equivalence.yaml",
]


def copy_into_workspace(source: Path, workspace: Path) -> str:
    relative = source.relative_to(ROOT)
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(relative)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    workspace = Path(tempfile.mkdtemp(prefix=f"portal-yamato-generator-turn-{args.turn:03d}-{stamp}-"))
    plan = CURRENT / "turns" / f"turn-{args.turn:03d}-plan.md"
    report = CURRENT / "turns" / f"turn-{args.turn:03d}-report.md"
    copied = [copy_into_workspace(path, workspace) for path in ALLOWED_PATHS if path.exists()]
    copied.append(copy_into_workspace(plan, workspace))

    context_path = CURRENT / "turns" / f"turn-{args.turn:03d}-generator-context.json"
    context = {
        "agent_id": f"codex-exec-generator-{stamp}",
        "agent_type": "generator",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "input_files": copied,
        "forbidden_files": [
            ".loop/current/state.json",
            ".loop/current/feedback.md",
            ".loop/current/turns/*-eval.json",
            ".loop/current/snapshots/",
            "validation/runs/",
        ],
        "context_strategy": "sanitized-generator-workspace",
        "previous_thread_reused": False,
        "sandbox_mode": "workspace-write",
        "workspace": str(workspace),
        "report_file": str(report),
        "finished_at": None,
    }

    if args.dry_run:
        context["finished_at"] = datetime.now().isoformat(timespec="seconds")
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"dry_run": True, "workspace": str(workspace), "context": str(context_path)}, ensure_ascii=False))
        return 0

    prompt = (
        f"Work only inside this sanitized workspace. Read turn plan at {plan.relative_to(ROOT)}. "
        "Do not read outside the current directory. Run the checks requested in the plan when possible. "
        "Write a concise generator report in the workspace as generator-report.md. "
        "Do not assign scores or edit criteria/schema/reference outputs."
    )
    env = os.environ.copy()
    env["EVAL_LOOP_ROLE"] = "generator"
    completed = subprocess.run(
        [
            "codex",
            "exec",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            str(workspace),
            prompt,
        ],
        cwd=workspace,
        env=env,
        text=True,
        capture_output=True,
        timeout=600,
    )
    context["finished_at"] = datetime.now().isoformat(timespec="seconds")
    context["returncode"] = completed.returncode
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    workspace_report = workspace / "generator-report.md"
    if workspace_report.exists():
        shutil.copy2(workspace_report, report)
    if completed.returncode != 0:
        print(completed.stdout, file=sys.stderr)
        print(completed.stderr, file=sys.stderr)
        return completed.returncode
    print(json.dumps({"workspace": str(workspace), "context": str(context_path), "report": str(report)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
