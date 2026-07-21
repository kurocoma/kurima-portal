"""請求関連取得の集約・manifest参照層。

一次情報: Obsidian「精算・請求・受取明細取得手順.md」「Playwright実装仕様.md」
「BillPay精算データ取得手順.md」「BillPay Playwright実装仕様.md」（2026-07-12 観測）。
実サイトへの接続・ログインは未検証（認証情報なし）。モール別モジュールの
DOM操作は観測済み契約を転記しており、初回実行時に実地検証が必要。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from portal_app.services.billing_statements_rakuten import (
    BillPaySettlementResult,
    download_billpay_settlement_sync,
)
from portal_app.services.billing_statements_yahoo import (
    YahooBillingStatementsResult,
    download_yahoo_statements_sync,
)
from portal_app.services.paths import find_billing_statements_paths


_MANIFEST_FIELDS = frozenset(
    {
        "artifact_id",
        "batch_id",
        "mall",
        "category",
        "statement_type",
        "account_fingerprint",
        "target_label",
        "state",
        "closing_date_label",
        "screen",
        "scope",
        "document_type",
        "document_kind",
        "issue_date",
        "filename",
        "relative_path",
        "identity_hash",
        "sha256",
        "row_count",
        "validated",
        "warnings",
        "fetched_at",
    }
)
_TYPE_LABELS = {
    "billing": "請求明細",
    "receipt": "受取明細",
    "settlement": "精算明細",
}
_DOCUMENT_LABELS = {
    "summary": "表示情報CSV",
    "34": "店舗別内訳書CSV",
    "33": "請求確認CSV",
    "32": "精算書PDF",
    "11": "請求書PDF",
    "31": "請求関連PDF",
    "41": "帳票PDF",
    "51": "関連帳票ZIP",
    "52": "関連帳票ZIP",
    "72": "帳票PDF",
    "74": "関連CSV",
}
_VALID_STATES = {"final", "provisional", "unknown", "no_data"}


def read_billing_manifest() -> list[dict[str, object]]:
    manifest = find_billing_statements_paths().manifest_path
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def append_billing_manifest(record: dict[str, object]) -> None:
    """金額・実ID・社名を受け付けないallowlistでmanifestへ追記する。"""

    safe_record = {key: value for key, value in record.items() if key in _MANIFEST_FIELDS}
    if not safe_record.get("mall") or not safe_record.get("category"):
        raise ValueError("mall と category は必須です。")
    manifest = find_billing_statements_paths().manifest_path
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(safe_record, ensure_ascii=False, sort_keys=True) + "\n")


def _format_fetched_at(value: object) -> str:
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M")


def _target_month_label(value: object) -> str:
    raw = str(value or "")
    if len(raw) == 7 and raw[4] == "-":
        return f"{raw[:4]}年{int(raw[5:])}月"
    return raw


def _latest_batch(records: list[dict[str, object]], mall: str) -> list[dict[str, object]]:
    mall_records = [record for record in records if record.get("mall") == mall]
    if mall == "yahoo":
        account = os.environ.get("KURIMA_YAHOO_STORE_ACCOUNT", "").strip()
        if not account:
            return []
        fingerprint = hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]
        mall_records = [
            record
            for record in mall_records
            if record.get("account_fingerprint") == fingerprint
        ]
    if not mall_records:
        return []
    latest = next(
        (
            record
            for record in reversed(mall_records)
            if record.get("category") == "batch_complete"
        ),
        None,
    )
    if latest is None:
        return []
    batch_id = latest.get("batch_id")
    if batch_id:
        return [record for record in mall_records if record.get("batch_id") == batch_id]
    return [latest]


def _normalise_state(value: object) -> str:
    state = str(value or "unknown")
    return state if state in _VALID_STATES else "unknown"


def read_billing_preview(tab: str) -> dict[str, object] | None:
    if tab not in {"yahoo", "rakuten"}:
        raise ValueError("tab は yahoo または rakuten を指定してください。")
    batch = _latest_batch(read_billing_manifest(), tab)
    if not batch:
        return None
    latest = batch[-1]
    staging_dir = str(find_billing_statements_paths().staging_dir)
    fetched_at_label = _format_fetched_at(latest.get("fetched_at"))

    if tab == "yahoo":
        files: list[dict[str, object]] = []
        warnings: list[str] = [
            str(value) for value in (latest.get("warnings") or []) if str(value)
        ]
        seen_warnings: set[str] = set()
        seen_warnings.update(warnings)
        for record in batch:
            statement_type = str(record.get("statement_type") or "")
            if statement_type not in _TYPE_LABELS:
                continue
            state = _normalise_state(record.get("state"))
            if not record.get("artifact_id") and state in {"provisional", "unknown"}:
                type_label = _TYPE_LABELS[statement_type]
                if state == "provisional":
                    warning = (
                        f"{type_label}は未確定のため取得していません。"
                        "確定後に再実行してください。"
                    )
                else:
                    warning = (
                        f"{type_label}の確定状態を確認できなかったため取得していません。"
                        "画面を確認して再実行してください。"
                    )
                if warning not in seen_warnings:
                    seen_warnings.add(warning)
                    warnings.append(warning)
            sha256 = str(record.get("sha256") or "")
            files.append(
                {
                    "type_label": _TYPE_LABELS[statement_type],
                    "state": state,
                    "row_count": (
                        int(record["row_count"])
                        if record.get("row_count") is not None
                        else None
                    ),
                    "artifact_id": (
                        str(record["artifact_id"])
                        if record.get("artifact_id")
                        else None
                    ),
                    "filename": (
                        str(record["filename"]) if record.get("filename") else None
                    ),
                    "sha8": sha256[:8],
                    "closing_date_label": (
                        str(record["closing_date_label"])
                        if record.get("closing_date_label")
                        else None
                    ),
                }
            )
        return {
            "target_month_label": _target_month_label(latest.get("target_label")),
            "fetched_at_label": fetched_at_label,
            "warnings": warnings,
            "staging_dir": staging_dir,
            "files": files,
        }

    documents: list[dict[str, object]] = []
    for record in batch:
        document_type = str(record.get("document_type") or "")
        if not record.get("artifact_id"):
            continue
        sha256 = str(record.get("sha256") or "")
        documents.append(
            {
                "issue_date": str(record.get("issue_date") or ""),
                "doc_label": _DOCUMENT_LABELS.get(
                    document_type, f"document-type {document_type}"
                ),
                "validated": bool(record.get("validated")),
                "artifact_id": str(record.get("artifact_id") or ""),
                "filename": str(record.get("filename") or ""),
                "sha8": sha256[:8],
            }
        )
    return {
        "fetched_at_label": fetched_at_label,
        "warnings": list(dict.fromkeys(latest.get("warnings") or [])),
        "staging_dir": staging_dir,
        "documents": documents,
    }


def resolve_artifact_path(artifact_id: str) -> Path | None:
    """manifestに記録されたartifactだけを請求データ保存ルート内で解決する。"""

    paths = find_billing_statements_paths()
    root = paths.root.resolve()
    record = next(
        (
            item
            for item in reversed(read_billing_manifest())
            if item.get("artifact_id") == artifact_id
        ),
        None,
    )
    if record is None or not record.get("relative_path"):
        return None
    if record.get("mall") == "yahoo":
        account = os.environ.get("KURIMA_YAHOO_STORE_ACCOUNT", "").strip()
        fingerprint = (
            hashlib.sha256(account.encode("utf-8")).hexdigest()[:16]
            if account
            else None
        )
        if record.get("account_fingerprint") != fingerprint:
            return None
    candidate = (root / str(record["relative_path"])).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def latest_artifact_paths(mall: str) -> list[tuple[str, Path]]:
    """一括ZIP用に最新batchの安全なartifactだけを返す。"""

    if mall not in {"yahoo", "rakuten"}:
        return []
    records = _latest_batch(read_billing_manifest(), mall)
    resolved: list[tuple[str, Path]] = []
    for record in records:
        artifact_id = str(record.get("artifact_id") or "")
        path = resolve_artifact_path(artifact_id)
        if path is not None:
            resolved.append((str(record.get("filename") or path.name), path))
    return resolved


__all__ = [
    "BillPaySettlementResult",
    "YahooBillingStatementsResult",
    "append_billing_manifest",
    "download_billpay_settlement_sync",
    "download_yahoo_statements_sync",
    "latest_artifact_paths",
    "read_billing_manifest",
    "read_billing_preview",
    "resolve_artifact_path",
]
