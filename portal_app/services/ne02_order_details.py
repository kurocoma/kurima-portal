from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from portal_app.services.next_engine_downloader import APP_ROOT
from portal_app.services.paths import find_portal_paths


FLOW_ID = "87845dc2-b5fd-4a31-aafd-04980b5ceb61"
FLOW_NAME = "ne02_受注明細ダウンロード_sharepoint移行後_2"
AUDIT_LOG_DIR = APP_ROOT / "logs" / "ne02_order_details"
AUDIT_LOG_PATH = AUDIT_LOG_DIR / "ne02_order_detail_download_audit.jsonl"


@dataclass(frozen=True)
class Ne02OrderDetailStep:
    subflow: str
    target: str
    status: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class Ne02OrderDetailDownloadResult:
    captured_at: datetime
    executed: bool
    flow_id: str
    flow_name: str
    target_date: str | None
    order_numbers: tuple[str, ...]
    source_sample_input: Path | None
    expected_contract: Path | None
    steps: tuple[Ne02OrderDetailStep, ...]
    downloaded_file: Path | None
    output_rows: int | None
    skipped_reason: str | None
    audit_path: Path | None


def download_ne02_order_details_sync(
    *,
    execute: bool,
    target_date: str | None = None,
    order_numbers: Iterable[str] = (),
    sample_input: str | Path | None = None,
    expected_contract: str | Path | None = None,
    write_audit: bool = True,
) -> Ne02OrderDetailDownloadResult:
    """Plan or execute the NE02 order-detail download replacement."""

    sample_path = Path(sample_input) if sample_input else None
    expected_path = Path(expected_contract) if expected_contract else None
    records = tuple(_clean_records(order_numbers)) or _records_from_sample(sample_path)
    skipped_reason = None
    executed = False
    if execute:
        skipped_reason = (
            "live NE02 order-detail download is not implemented yet; "
            "complete download selectors, file contract, and G7 expected outputs first"
        )

    result = Ne02OrderDetailDownloadResult(
        captured_at=datetime.now(),
        executed=executed,
        flow_id=FLOW_ID,
        flow_name=FLOW_NAME,
        target_date=target_date or _target_date_from_sample(sample_path),
        order_numbers=records,
        source_sample_input=sample_path,
        expected_contract=expected_path,
        steps=_planned_steps(),
        downloaded_file=None,
        output_rows=None,
        skipped_reason=skipped_reason,
        audit_path=AUDIT_LOG_PATH if write_audit else None,
    )
    if write_audit:
        _append_audit(result)
    return result


def _planned_steps() -> tuple[Ne02OrderDetailStep, ...]:
    return (
        Ne02OrderDetailStep(
            subflow="Main",
            target="portal_tool/portal_app/services/ne02_order_details.py",
            status="dry_run_ready",
            notes=("Orchestrates the NE02 order-detail replacement boundary.",),
        ),
        Ne02OrderDetailStep(
            subflow="Ne_to_Yamato_import",
            target="portal_tool/portal_app/services/yamato_conversion.py",
            status="planned",
            notes=("Reuse direct CSV conversion instead of Excel UI import.",),
        ),
        Ne02OrderDetailStep(
            subflow="haiso_only_download",
            target="Next Engine Playwright download",
            status="planned",
            notes=("Fix search conditions, output file pattern, and encoding before execute mode.",),
        ),
        Ne02OrderDetailStep(
            subflow="downloadform",
            target="explicit download handling",
            status="planned",
            notes=("Replace PAD save dialog with deterministic download path handling.",),
        ),
        Ne02OrderDetailStep(
            subflow="NeDownload",
            target="portal_tool/portal_app/services/next_engine_downloader.py",
            status="verify_existing",
            notes=("Existing downloader is the likely base implementation; G7 contracts must approve exact filters.",),
        ),
    )


def _clean_records(values: Iterable[str]) -> list[str]:
    return [value.strip() for value in values if str(value).strip()]


def _records_from_sample(sample_path: Path | None) -> tuple[str, ...]:
    if not sample_path or not sample_path.is_file():
        return tuple()
    doc = json.loads(sample_path.read_text(encoding="utf-8"))
    records = doc.get("inputs", {}).get("target_records", [])
    if not isinstance(records, list):
        return tuple()
    return tuple(value for value in _clean_records(str(item) for item in records) if not value.startswith("${"))


def _target_date_from_sample(sample_path: Path | None) -> str | None:
    if not sample_path or not sample_path.is_file():
        return None
    doc = json.loads(sample_path.read_text(encoding="utf-8"))
    target_date = doc.get("inputs", {}).get("target_date")
    if not isinstance(target_date, str) or target_date.startswith("${"):
        return None
    return target_date


def _append_audit(result: Ne02OrderDetailDownloadResult) -> None:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = _json_safe(asdict(result))
    with AUDIT_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _json_safe(value):
    if isinstance(value, Path):
        return _sanitize_path(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _sanitize_path(path: Path) -> str:
    raw = str(path)
    replacements = [(str(APP_ROOT), "${APP_ROOT}"), (str(Path.home()), "${USER_HOME}")]
    try:
        replacements.insert(0, (str(find_portal_paths().portal_root), "${PORTAL_ROOT}"))
    except Exception:
        pass
    for needle, replacement in replacements:
        if needle and raw.startswith(needle):
            return replacement + raw[len(needle) :]
    return raw
