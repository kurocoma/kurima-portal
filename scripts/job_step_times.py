"""実行ログ(events.jsonl)からステップ別所要時間を集計する検証ツール。

使い方:
  uv run python scripts/job_step_times.py                     # clickpost_full_run の最新実行
  uv run python scripts/job_step_times.py --workflow clickpost_prepare
  uv run python scripts/job_step_times.py --run-dir logs/execution_runs/clickpost_full_run/<run>
  uv run python scripts/job_step_times.py --last 2            # 直近2件を並べて比較
  uv run python scripts/job_step_times.py --verbose           # 区間別（決済1件ごと等）の内訳も表示

速度改善の before/after 比較に使う。logged_at は秒精度。
決済1件ごとの detail 更新や invoice_download のフェーズ境界（復旧開始など）も
running イベントとして記録されるため、--verbose で件別・フェーズ別の内訳が見える。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = APP_ROOT / "logs" / "execution_runs"


def _load_events(run_dir: Path) -> list[dict]:
    events_path = run_dir / "events.jsonl"
    if not events_path.is_file():
        raise FileNotFoundError(f"events.jsonl が見つかりません: {events_path}")
    events = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _intervals(events: list[dict]) -> tuple[list[tuple[str, str, float]], float | None]:
    """running イベントごとの区間 (step, detail, 所要秒) と全体秒を返す。

    区間は「その running イベントの時刻」から「次の running イベントか job 終了」まで。
    同一ステップ内の detail 更新（決済1件ごと等）も1区間として扱う。
    """
    parsed: list[tuple[datetime, dict]] = []
    for event in events:
        logged_at = event.get("logged_at")
        if not logged_at:
            continue
        try:
            parsed.append((datetime.fromisoformat(logged_at), event))
        except ValueError:
            continue
    if not parsed:
        return [], None

    job_start = parsed[0][0]
    job_end = parsed[-1][0]

    marks: list[tuple[datetime, str, str]] = []
    for ts, event in parsed:
        if event.get("event") == "step_updated" and event.get("status") == "running":
            marks.append((ts, event.get("step") or "?", str(event.get("detail") or "")))

    intervals: list[tuple[str, str, float]] = []
    for index, (ts, step, detail) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else job_end
        intervals.append((step, detail, (end - ts).total_seconds()))
    return intervals, (job_end - job_start).total_seconds()


def _print_run(run_dir: Path, *, verbose: bool) -> None:
    events = _load_events(run_dir)
    intervals, total = _intervals(events)
    meta = {}
    summary_path = run_dir / "summary.json"
    if summary_path.is_file():
        try:
            meta = json.loads(summary_path.read_text(encoding="utf-8")).get("metadata", {}) or {}
        except (json.JSONDecodeError, OSError):
            meta = {}
    print(f"=== {run_dir.parent.name} / {run_dir.name} ===")
    if meta:
        keys = ("browser_mode", "slow_mo_ms", "max_payments", "restore_ne_status")
        print("  " + "  ".join(f"{key}={meta[key]}" for key in keys if key in meta))

    if verbose:
        for step, detail, seconds in intervals:
            bar = "#" * min(60, int(seconds))
            print(f"  {step:<20} {seconds:>7.1f}s  {bar}  {detail[:48]}")
    else:
        # ステップ単位に合算（登場順を保つ）
        order: list[str] = []
        sums: dict[str, float] = {}
        for step, _detail, seconds in intervals:
            if step not in sums:
                order.append(step)
                sums[step] = 0.0
            sums[step] += seconds
        for step in order:
            seconds = sums[step]
            bar = "#" * min(60, int(seconds))
            print(f"  {step:<20} {seconds:>7.1f}s  {bar}")
    if total is not None:
        print(f"  {'TOTAL':<20} {total:>7.1f}s")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", default="clickpost_full_run")
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--last", type=int, default=1)
    parser.add_argument("--verbose", action="store_true", help="区間別（決済1件ごと等）の内訳を表示")
    args = parser.parse_args()

    if args.run_dir is not None:
        _print_run(args.run_dir, verbose=args.verbose)
        return 0

    workflow_dir = RUNS_ROOT / args.workflow
    if not workflow_dir.is_dir():
        print(f"実行ログがありません: {workflow_dir}", file=sys.stderr)
        return 1
    run_dirs = sorted((d for d in workflow_dir.iterdir() if d.is_dir()), key=lambda d: d.name)
    for run_dir in run_dirs[-args.last:]:
        _print_run(run_dir, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
