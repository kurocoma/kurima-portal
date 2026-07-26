"""NE納品書PDFのページ順解析（2026-07-26 レターパック並び順対応）。

NEが一括出力する納品書PDFのページ順は、受注一覧CSVの並び（伝票番号降順）とも
レターパック住所録の従来並び（店舗降順→伝票番号昇順）とも一致しない独自順
（実測: 楽天→yahoo のグループ内も伝票番号順ではない）。納品書とシールを同じ
順で突き合わせられるよう、PDFの実ページ順から伝票番号の並びを読み取る。

抽出は「既知の伝票番号集合との突合」方式。ページ本文には受注番号断片
（例: 235545）や郵便番号など紛らわしい数字が多いため、パターン推測ではなく
呼び出し元が持つ候補（購入者データCSVの伝票番号）だけを探す。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

PAGE_SEPARATOR = "\f"  # pdfminer の extract_text はページ境界に form feed を入れる


def extract_invoice_denpyo_order(
    pdf_path: Path,
    known_denpyo_numbers: Iterable[str],
) -> list[str]:
    """納品書PDFのページ順に、既知の伝票番号を出現順で返す。

    - 伝票番号が見つからないページ（明細続きページ）は前ページの続きとして無視する。
    - 1ページに複数の既知番号が載る場合は判定不能としてそのページを無視する
      （該当伝票は呼び出し元のフォールバック順に落ちる）。
    - 同じ番号が複数ページに出ても最初の出現位置を採用する。
    - pdfminer は import が重いため関数内で遅延 import する。
    """
    from pdfminer.high_level import extract_text

    known = [str(number).strip() for number in known_denpyo_numbers if str(number).strip()]
    if not known:
        return []
    patterns = {number: re.compile(rf"(?<!\d){re.escape(number)}(?!\d)") for number in known}

    text = extract_text(str(pdf_path)) or ""
    ordered: list[str] = []
    seen: set[str] = set()
    for page_text in text.split(PAGE_SEPARATOR):
        hits = [number for number, pattern in patterns.items() if pattern.search(page_text)]
        if len(hits) != 1:
            continue
        number = hits[0]
        if number in seen:
            continue
        seen.add(number)
        ordered.append(number)
    return ordered
