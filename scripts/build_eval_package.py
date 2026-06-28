from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def build_package(*, turn: int, hard_gate_report: Path, output_dir: Path | None = None) -> dict:
    report = json.loads(hard_gate_report.read_text(encoding="utf-8"))
    package_dir = output_dir or ROOT / ".loop" / "current" / "eval-packages" / f"turn-{turn:03d}"
    if package_dir.exists():
        shutil.rmtree(package_dir)
    package_dir.mkdir(parents=True)

    copy_file(ROOT / ".loop" / "current" / "task.md", package_dir / "task.md")
    copy_file(ROOT / ".loop" / "current" / "criteria.yaml", package_dir / "criteria.yaml")
    copy_file(ROOT / ".loop" / "current" / "eval-schema.json", package_dir / "eval-schema.json")
    copy_file(hard_gate_report, package_dir / "validation-report.json")

    generated_csv = Path(str(report["generated_csv"]))
    reference_csv = Path(str(report["reference_csv"]))
    copy_file(generated_csv, package_dir / "artifacts" / generated_csv.name)
    copy_file(reference_csv, package_dir / "reference" / reference_csv.name)

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "turn": turn,
        "package_dir": str(package_dir),
        "input_files": [
            "task.md",
            "criteria.yaml",
            "eval-schema.json",
            "validation-report.json",
            f"artifacts/{generated_csv.name}",
            f"reference/{reference_csv.name}",
        ],
        "forbidden_files": [
            ".loop/current/state.json",
            ".loop/current/feedback.md",
            ".loop/current/turns/*-plan.md",
            ".loop/current/turns/*-report.md",
            ".loop/current/turns/*-eval.json",
            ".loop/current/snapshots/",
            "generator conversation",
            "previous score",
            "best score",
        ],
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--hard-gate-report", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    manifest = build_package(turn=args.turn, hard_gate_report=args.hard_gate_report, output_dir=args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
