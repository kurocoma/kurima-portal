"""レターパック配送番号反映（依頼4・2026-07-20）— スキャン値の判定と2列CSV出力。

既存Excelマクロ「レターパック送り状作成.xlsm」の置き換え。確定仕様（ユーザー回答）:
- 作業範囲は 2列CSV（伝票番号,送り状番号）の作成まで。NEへの反映は既存の
  出荷確定カードが「完成したデータ」フォルダの *レターパック*.csv を自動読込して行う
  （shipment_confirmation._load_tracking_maps）。そのためExcelと同一の
  保存先・命名（yyyyMMddhhmmレターパック.csv）・文字コード(cp932)で出力する。
- 入力欄は1つで自動判定: 「D＋数字」= 納品書（伝票番号。D・先頭ゼロを除去）、
  「12桁数字（A接頭辞可）」= レターパック送り状番号（Aを除去）。
  形式はExcelマクロのVBA（D除去/A除去）と実出力CSVで確認済み。
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from portal_app.services.paths import PortalPaths, find_portal_paths
from portal_app.services.shipment_confirmation import normalize_barcode_value

TOOL_DIR_NAME = "CP・LPP宛名作成ツール"
COMPLETE_DIR_NAME = "完成したデータ"
CSV_HEADERS = ("伝票番号", "送り状番号")
TRACKING_LENGTH = 12  # レターパックの送り状番号（お問い合わせ番号）は12桁
DENPYO_MAX_LENGTH = 8  # 伝票番号は現状5桁。桁あふれ余裕を見て8桁までを伝票番号と判定


@dataclass(frozen=True)
class ScanClassification:
    kind: str  # "denpyo" / "tracking" / "unknown"
    value: str
    reason: str | None = None


def classify_scan_value(raw: object) -> ScanClassification:
    """スキャン/手入力された1値を「納品書(伝票番号)」「送り状番号」に自動判定する。

    - D始まり（納品書バーコード）→ 伝票番号。normalize_barcode_value でD・ゼロ埋め除去
    - A始まり+12桁数字（レターパックのバーコード）→ 送り状番号（A除去）
    - 12桁数字 → 送り状番号
    - 8桁以下の数字 → 伝票番号（手入力を許容）
    - それ以外 → unknown
    """
    text = str(raw or "").strip()
    if not text:
        return ScanClassification("unknown", "", "空の入力です")

    if text[:1] in ("D", "d"):
        value = normalize_barcode_value(text)
        if value.isdigit():
            return ScanClassification("denpyo", value)
        return ScanClassification("unknown", text, "D始まりですが数字を読み取れません")

    body = text[1:] if text[:1] in ("A", "a") else text
    body = re.sub(r"\s", "", body)
    if body.isdigit():
        if len(body) == TRACKING_LENGTH:
            return ScanClassification("tracking", body)
        if text[:1] in ("A", "a"):
            return ScanClassification(
                "unknown", text, f"A始まりの送り状番号は{TRACKING_LENGTH}桁のはずです"
            )
        if len(body) <= DENPYO_MAX_LENGTH:
            return ScanClassification("denpyo", normalize_barcode_value(body))
        return ScanClassification("unknown", text, "桁数が伝票番号にも送り状番号にも一致しません")

    return ScanClassification("unknown", text, "数字を読み取れません")


def default_output_dir(paths: PortalPaths | None = None) -> Path:
    resolved = paths or find_portal_paths()
    return resolved.portal_root / TOOL_DIR_NAME / COMPLETE_DIR_NAME


def letterpack_csv_filename(now: datetime) -> str:
    """Excelマクロと同一の命名: Format(Now,"yyyymmddhhmm") & "レターパック.csv"。"""
    return f"{now:%Y%m%d%H%M}レターパック.csv"


def write_letterpack_csv(
    pairs: list[dict[str, str]],
    *,
    output_dir: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """伝票番号と送り状番号のペア一覧を、Excel互換の2列CSV(cp932)として書き出す。

    - 同一伝票番号は最後のペアを採用する（画面側も上書き運用。読込側の
      setdefault=最初の1件採用に対し、書き出し時点で一意化して事故を防ぐ）。
    - 同じ「分」に既にファイルがあれば秒付きの別名にして上書き消失を防ぐ。
    """
    cleaned: dict[str, str] = {}
    for pair in pairs:
        denpyo = normalize_barcode_value(str(pair.get("denpyo", "")))
        tracking = re.sub(r"\D", "", str(pair.get("tracking", "")))
        if not denpyo or not denpyo.isdigit():
            raise ValueError(f"伝票番号を解釈できません: {pair.get('denpyo')!r}")
        if len(tracking) != TRACKING_LENGTH:
            raise ValueError(
                f"送り状番号は{TRACKING_LENGTH}桁の数字で指定してください: {pair.get('tracking')!r}"
            )
        cleaned[denpyo] = tracking
    if not cleaned:
        raise ValueError("出力するペアがありません。")

    stamp = now or datetime.now()
    directory = output_dir if output_dir is not None else default_output_dir()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / letterpack_csv_filename(stamp)
    if destination.exists():
        destination = directory / f"{stamp:%Y%m%d%H%M%S}レターパック.csv"

    with destination.open("w", encoding="cp932", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(CSV_HEADERS)
        for denpyo, tracking in cleaned.items():
            writer.writerow([denpyo, tracking])
    return destination
