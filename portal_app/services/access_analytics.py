"""アクセス解析取得の集約・manifest参照層。

一次情報: Obsidian「楽天市場-デバイス別アクセス数取得手順.md」および
「Yahoo!ショッピング-デバイス別アクセス数取得手順.md」（2026-07-12 観測）。
実サイトへの接続・ログインは未検証（認証情報なし）。モール別モジュールの
DOM操作は観測済み契約を転記しており、初回実行時に実地検証が必要。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from portal_app.services.access_analytics_rakuten import (
    RakutenDeviceAccessResult,
    download_rakuten_device_access_sync,
)
from portal_app.services.access_analytics_yahoo import (
    YahooAccessAnalyticsResult,
    download_yahoo_access_reports_sync,
)
from portal_app.services.paths import find_access_analytics_paths


_MANIFEST_FIELDS = frozenset(
    {
        "artifact_id",
        "batch_id",
        "mall",
        "category",
        "filename",
        "relative_path",
        "sha256",
        "row_count",
        "device",
        "device_label",
        "account_fingerprint",
        "target_label",
        "fetched_at",
    }
)
_RAKUTEN_DEVICE_ORDER = {
    "pc": 0,
    "app": 1,
    "smartphone_web": 2,
    "all": 3,
}
_YAHOO_DEVICE_ORDER = {
    "pc": 0,
    "smartphone_web": 1,
    "app": 2,
    "all": 3,
}


def read_access_analytics_manifest() -> list[dict[str, object]]:
    manifest = find_access_analytics_paths().manifest_path
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


def append_access_analytics_manifest(record: dict[str, object]) -> None:
    """非機密allowlistフィールドだけをmanifestへ1行追記する。"""

    safe_record = {key: value for key, value in record.items() if key in _MANIFEST_FIELDS}
    if safe_record.get("category") != "batch_complete" and (
        not safe_record.get("artifact_id") or not safe_record.get("relative_path")
    ):
        raise ValueError("artifact_id と relative_path は必須です。")
    manifest = find_access_analytics_paths().manifest_path
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(safe_record, ensure_ascii=False, sort_keys=True) + "\n")


def record_access_analytics_batch(
    *,
    mall: str,
    batch_id: str,
    target_label: str,
    artifacts: list[tuple[str, str]],
) -> None:
    """既存artifactをコピーせず、複数回取得を1つの完了batchとして参照する。"""

    records = read_access_analytics_manifest()
    selected: list[dict[str, object]] = []
    for filename, sha256 in artifacts:
        record = next(
            (
                item
                for item in reversed(records)
                if item.get("mall") == mall
                and item.get("filename") == filename
                and item.get("sha256") == sha256
            ),
            None,
        )
        if record is None:
            raise ValueError("完了batchへ束ねるartifactがmanifestにありません。")
        clone = dict(record)
        clone["batch_id"] = batch_id
        clone["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        append_access_analytics_manifest(clone)
        selected.append(clone)
    append_access_analytics_manifest(
        {
            "artifact_id": None,
            "batch_id": batch_id,
            "mall": mall,
            "category": "batch_complete",
            "filename": None,
            "relative_path": None,
            "sha256": None,
            "row_count": sum(int(item.get("row_count") or 0) for item in selected),
            "device": None,
            "device_label": None,
            "target_label": target_label,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        }
    )


def _format_fetched_at(value: object) -> str:
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return parsed.strftime("%Y-%m-%d %H:%M")


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


def _file_view(record: dict[str, object]) -> dict[str, object]:
    sha256 = str(record.get("sha256") or "")
    return {
        "device_label": str(record.get("device_label") or record.get("device") or ""),
        "row_count": int(record.get("row_count") or 0),
        "artifact_id": str(record.get("artifact_id") or ""),
        "filename": str(record.get("filename") or ""),
        "sha8": sha256[:8],
        "target_label": str(record.get("target_label") or ""),
    }


def read_access_analytics_preview(tab: str) -> dict[str, object] | None:
    if tab not in {"rakuten", "yahoo"}:
        raise ValueError("tab は rakuten または yahoo を指定してください。")
    batch = _latest_batch(read_access_analytics_manifest(), tab)
    if not batch:
        return None
    latest = batch[-1]
    common: dict[str, object] = {
        "fetched_at_label": _format_fetched_at(latest.get("fetched_at")),
        "target_label": str(latest.get("target_label") or ""),
        "staging_dir": str(find_access_analytics_paths().staging_dir),
    }
    if tab == "rakuten":
        records = [record for record in batch if record.get("category") == "device_access"]
        records.sort(
            key=lambda record: _RAKUTEN_DEVICE_ORDER.get(str(record.get("device")), 99)
        )
        return {**common, "files": [_file_view(record) for record in records]}

    product_record = next(
        (record for record in batch if record.get("category") == "product"),
        None,
    )
    if product_record is None:
        # 途中失敗したbatchを「取得済み」と表示せず、次回成功batchまで空表示にする。
        return None
    overall_records = [
        record for record in batch if record.get("category") == "overall"
    ]
    overall_records.sort(
        key=lambda record: _YAHOO_DEVICE_ORDER.get(str(record.get("device")), 99)
    )
    view = _file_view(product_record)
    product = {
        "row_count": view["row_count"],
        "artifact_id": view["artifact_id"],
        "filename": view["filename"],
        "sha8": view["sha8"],
    }
    return {
        **common,
        "product": product,
        "overall": [_file_view(record) for record in overall_records],
    }


def resolve_artifact_path(artifact_id: str) -> Path | None:
    """manifestに記録されたartifactだけを保存ルート内で解決する。"""

    paths = find_access_analytics_paths()
    root = paths.root.resolve()
    record = next(
        (
            item
            for item in reversed(read_access_analytics_manifest())
            if item.get("artifact_id") == artifact_id
        ),
        None,
    )
    if record is None:
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
    relative = record.get("relative_path")
    if not relative:
        return None
    candidate = (root / str(relative)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def latest_artifact_paths(mall: str) -> list[tuple[str, Path]]:
    """一括ZIP用に最新batchのartifact名と安全な実体だけを返す。"""

    if mall not in {"rakuten", "yahoo"}:
        return []
    records = _latest_batch(read_access_analytics_manifest(), mall)
    resolved: list[tuple[str, Path]] = []
    for record in records:
        artifact_id = str(record.get("artifact_id") or "")
        path = resolve_artifact_path(artifact_id)
        if path is not None:
            resolved.append((str(record.get("filename") or path.name), path))
    return resolved


__all__ = [
    "RakutenDeviceAccessResult",
    "YahooAccessAnalyticsResult",
    "append_access_analytics_manifest",
    "download_rakuten_device_access_sync",
    "download_yahoo_access_reports_sync",
    "latest_artifact_paths",
    "read_access_analytics_manifest",
    "read_access_analytics_preview",
    "record_access_analytics_batch",
    "resolve_artifact_path",
]
