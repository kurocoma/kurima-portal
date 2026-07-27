"""高江洲発注表（編集用）の保存・読込。

高江洲発注書の集計結果をベースに、ポータル画面上で数量変更・行の追加/削除が
できる編集レイヤー（2026-07-28 使用者要望: メールへそのまま貼れる表が欲しい、
頼みすぎ・紙パック除外などの編集がしたい）。

- 編集内容は data/takaesu/order_table.json に自動保存する（atomic write）。
- 新しい在庫データを取り込んでも自動では作り直さない。
  「最新の発注書から作り直す」操作（reset_order_table）でのみ再生成する。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from portal_app.services.takaesu_orders import (
    TAKAESU_OUTPUT_HEADERS,
    preview_takaesu_order_sheet,
)

APP_ROOT = Path(__file__).resolve().parents[2]
ORDER_TABLE_PATH = APP_ROOT / "data" / "takaesu" / "order_table.json"

TABLE_COLUMNS: tuple[str, ...] = tuple(TAKAESU_OUTPUT_HEADERS)
MAX_ROWS = 1000
MAX_CELL_LENGTH = 500


@dataclass(frozen=True)
class TakaesuOrderTable:
    rows: tuple[dict[str, str], ...]
    base_source: str | None
    base_created_at: str | None
    updated_at: str | None


def load_order_table() -> TakaesuOrderTable | None:
    if not ORDER_TABLE_PATH.is_file():
        return None
    try:
        doc = json.loads(ORDER_TABLE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return None
    return TakaesuOrderTable(
        rows=tuple(_normalize_row(row) for row in rows[:MAX_ROWS] if isinstance(row, dict)),
        base_source=_text_or_none(doc.get("base_source")),
        base_created_at=_text_or_none(doc.get("base_created_at")),
        updated_at=_text_or_none(doc.get("updated_at")),
    )


def save_order_table_rows(rows: list[dict[str, object]]) -> TakaesuOrderTable:
    """編集中の行を保存する（自動保存の受け口）。ベース情報は保持する。"""
    current = load_order_table()
    return _write_table(
        rows,
        base_source=current.base_source if current else None,
        base_created_at=current.base_created_at if current else None,
    )


def reset_order_table() -> TakaesuOrderTable:
    """最新の高江洲発注書集計から表を作り直す（編集内容は破棄）。"""
    sheet = preview_takaesu_order_sheet(preview_limit=MAX_ROWS)
    rows = [
        {column: str(row.get(column, "")) for column in TABLE_COLUMNS}
        for row in sheet.preview_rows
    ]
    return _write_table(
        rows,
        base_source=sheet.source_csv.name if sheet.source_csv else None,
        base_created_at=datetime.now().isoformat(timespec="seconds"),
    )


def ensure_order_table() -> TakaesuOrderTable:
    """保存済みの表があればそれを、無ければ最新集計から生成して返す。"""
    current = load_order_table()
    if current is not None:
        return current
    return reset_order_table()


def _write_table(
    rows: list[dict[str, object]],
    *,
    base_source: str | None,
    base_created_at: str | None,
) -> TakaesuOrderTable:
    if not isinstance(rows, list):
        raise ValueError("rows はリストで指定してください。")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"行数が上限（{MAX_ROWS}行）を超えています。")
    normalized = tuple(_normalize_row(row) for row in rows if isinstance(row, dict))
    table = TakaesuOrderTable(
        rows=normalized,
        base_source=base_source,
        base_created_at=base_created_at,
        updated_at=datetime.now().isoformat(timespec="seconds"),
    )
    payload = {
        "rows": list(table.rows),
        "base_source": table.base_source,
        "base_created_at": table.base_created_at,
        "updated_at": table.updated_at,
    }
    ORDER_TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = ORDER_TABLE_PATH.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp_path, ORDER_TABLE_PATH)
    return table


def _normalize_row(row: dict[str, object]) -> dict[str, str]:
    return {
        column: str(row.get(column, "") or "").strip()[:MAX_CELL_LENGTH]
        for column in TABLE_COLUMNS
    }


def _text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
