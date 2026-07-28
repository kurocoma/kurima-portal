"""OneDrive上のExcelを安全に読むためのスナップショットユーティリティ。

商品管理シート.xlsm などのマスタは OneDrive 共有フォルダにあり、
Excelでの編集・OneDrive同期・ポータルの読み込みが同時に起こり得る。
元ファイルを開いたまま解析すると、その間 Excel の保存や同期と衝突する
（逆に Excel やOneDrive側の状態によってはポータルが PermissionError になる）。

対策（2026-07-29 要望「ファイルのロックなどが起きないような配慮」）:
- 元ファイルにはローカル一時フォルダへのコピー（一瞬）でしか触らない。
  解析はコピーに対して行うため、元ファイルのハンドル保持時間が最小になる。
- コピーが PermissionError 等で失敗したら、間隔を広げながらリトライする
  （OneDrive同期・Excel保存の瞬間的なロックはこれで抜けられる）。
- コピーは shutil.copyfile（読み取り共有で開くため、Excelで開かれたままの
  ファイルでも通常は成功する）。

OneDrive上のブックを読む処理は必ず excel_read_snapshot を通すこと。
書き込み（例: クリックポストcsv変換.xlsm への貼り付け）はコピー戦略が
使えないため対象外 — 呼び出し元の PermissionError ハンドリングに委ねる。
"""

from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SNAPSHOT_DIR = Path(tempfile.gettempdir()) / "kurima_portal_excel_snapshots"
COPY_RETRIES = 5
COPY_RETRY_DELAY_SECONDS = 0.6


@contextmanager
def excel_read_snapshot(path: Path) -> Iterator[Path]:
    """path を一時フォルダへコピーし、そのコピーのパスを返す（終了時に削除）。"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Excelファイルが見つかりません: {source}")

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_path = SNAPSHOT_DIR / f"{uuid.uuid4().hex}_{source.name}"
    for attempt in range(1, COPY_RETRIES + 1):
        try:
            shutil.copyfile(source, snapshot_path)
            break
        except (PermissionError, OSError) as exc:
            if attempt == COPY_RETRIES:
                raise PermissionError(
                    f"{source.name} を読み取れませんでした"
                    "（OneDrive同期中またはExcelが排他ロック中の可能性があります。"
                    f"少し待ってから再実行してください）: {exc}"
                ) from exc
            time.sleep(COPY_RETRY_DELAY_SECONDS * attempt)

    try:
        yield snapshot_path
    finally:
        try:
            snapshot_path.unlink(missing_ok=True)
        except OSError:
            pass
