from __future__ import annotations

import csv
import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from portal_app.services.clickpost import (
    LETTERPACK_ADDRESS_HEADERS,
    create_letterpack_address_csv,
    find_clickpost_paths,
)
from portal_app.services.next_engine_downloader import APP_ROOT


LETTERPACK_PDF_AUDIT_LOG_DIR = APP_ROOT / "logs" / "clickpost"
LETTERPACK_PDF_AUDIT_LOG_PATH = LETTERPACK_PDF_AUDIT_LOG_DIR / "letterpack_pdf_audit.jsonl"

PAGE_WIDTH = 595.22
PAGE_HEIGHT = 842.0
ROWS_PER_PAGE = 4
COLUMNS_PER_PAGE = 2
LABELS_PER_PAGE = ROWS_PER_PAGE * COLUMNS_PER_PAGE

COLUMN_OFFSET = 275.4
ADDRESS_MAX_WIDTH = 210.0
NAME_MAX_WIDTH = 205.0
PHONE_MAX_WIDTH = 120.0

LABEL_FONT = "LetterPackLabelFont"
BODY_FONT = "LetterPackBodyFont"
LABEL_FONT_SIZE = 6.0
ZIP_PHONE_FONT_SIZE = 9.96
ADDRESS_FONT_SIZE = 11.04
NAME_FONT_SIZE = 14.04

ROW_Y = (
    {
        "addr_jp": 754.5203,
        "addr_en": 747.3203,
        "zip": 743.0003,
        "address1": 718.8803,
        "address2": 700.880684,
        "name_jp": 686.9603,
        "name_en": 679.7603,
        "name": 675.5603,
        "phone_jp": 662.8403,
        "phone_en1": 655.6403,
        "phone_en2": 648.4403,
        "phone": 649.6403,
    },
    {
        "addr_jp": 568.5203,
        "addr_en": 561.3203,
        "zip": 557.0003,
        "address1": 532.8803,
        "address2": 514.880684,
        "name_jp": 500.9603,
        "name_en": 493.7603,
        "name": 489.5603,
        "phone_jp": 476.8403,
        "phone_en1": 469.6403,
        "phone_en2": 462.4403,
        "phone": 463.6403,
    },
    {
        "addr_jp": 382.4003,
        "addr_en": 375.2003,
        "zip": 370.8803,
        "address1": 346.7603,
        "address2": 328.760684,
        "name_jp": 314.8403,
        "name_en": 307.6403,
        "name": 303.4403,
        "phone_jp": 290.7203,
        "phone_en1": 283.5203,
        "phone_en2": 276.3203,
        "phone": 277.5203,
    },
    {
        "addr_jp": 196.8803,
        "addr_en": 189.6803,
        "zip": 185.3603,
        "address1": 161.2403,
        "address2": 143.480252,
        "name_jp": 129.8003,
        "name_en": 122.6003,
        "name": 118.4003,
        "phone_jp": 105.6803,
        "phone_en1": 98.4803,
        "phone_en2": 91.2803,
        "phone": 92.4803,
    },
)


@dataclass(frozen=True)
class LetterPackLabelPdfResult:
    address_csv: Path
    output_pdf: Path | None
    output_rows: int
    page_count: int
    warnings: tuple[str, ...]
    preview_rows: tuple[dict[str, str], ...]
    audit_path: Path


def create_letterpack_label_pdf(
    *,
    address_csv: Path | None = None,
    output_pdf: Path | None = None,
    refresh_address_csv: bool = True,
    preview_limit: int = 20,
) -> LetterPackLabelPdfResult:
    paths = find_clickpost_paths()
    warnings: list[str] = []

    if refresh_address_csv and address_csv is None:
        address_result = create_letterpack_address_csv(preview_limit=preview_limit)
        warnings.extend(address_result.warnings)
        if address_result.output_csv is None:
            raise RuntimeError("letterpack_addressbook.csv を作成できませんでした。")
        address_csv = address_result.output_csv
    else:
        address_csv = address_csv or paths.completed_data_dir / "letterpack_addressbook.csv"

    rows = _read_letterpack_csv(address_csv)
    normalized_rows = [_normalize_row(row) for row in rows if _has_printable_name(row)]
    if len(normalized_rows) != len(rows):
        warnings.append("宛名が空のレターパック行をPDF出力から除外しました。")

    if not normalized_rows:
        result = LetterPackLabelPdfResult(
            address_csv=address_csv,
            output_pdf=None,
            output_rows=0,
            page_count=0,
            warnings=tuple(dict.fromkeys(warnings + ["レターパックPDFの出力対象がありません。"])),
            preview_rows=tuple(),
            audit_path=LETTERPACK_PDF_AUDIT_LOG_PATH,
        )
        _append_audit("letterpack_pdf", result)
        return result

    if output_pdf is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_pdf = paths.completed_data_dir / "letterpack_label_pdfs" / f"letterpack_labels_{timestamp}.pdf"

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    _write_letterpack_pdf(output_pdf, normalized_rows, warnings)
    result = LetterPackLabelPdfResult(
        address_csv=address_csv,
        output_pdf=output_pdf,
        output_rows=len(normalized_rows),
        page_count=math.ceil(len(normalized_rows) / LABELS_PER_PAGE),
        warnings=tuple(dict.fromkeys(warnings)),
        preview_rows=tuple(normalized_rows[:preview_limit]),
        audit_path=LETTERPACK_PDF_AUDIT_LOG_PATH,
    )
    _append_audit("letterpack_pdf", result)
    return result


def _write_letterpack_pdf(
    output_pdf: Path,
    rows: list[dict[str, str]],
    warnings: list[str],
) -> None:
    _register_fonts()
    pdf = canvas.Canvas(str(output_pdf), pagesize=(PAGE_WIDTH, PAGE_HEIGHT))
    pdf.setTitle("レターパック宛名")

    for index, row in enumerate(rows):
        if index and index % LABELS_PER_PAGE == 0:
            pdf.showPage()
        page_index = index % LABELS_PER_PAGE
        row_index = page_index // COLUMNS_PER_PAGE
        column_index = page_index % COLUMNS_PER_PAGE
        _draw_label(pdf, row, row_index=row_index, column_index=column_index, warnings=warnings)

    pdf.save()


def _draw_label(
    pdf: canvas.Canvas,
    row: dict[str, str],
    *,
    row_index: int,
    column_index: int,
    warnings: list[str],
) -> None:
    x_offset = COLUMN_OFFSET * column_index
    y = ROW_Y[row_index]

    pdf.setFillColorRGB(0, 0, 0)
    _draw_static_labels(pdf, x_offset=x_offset, y=y)

    zip_text = _zip_text(row.get("郵便番号", ""))
    _draw_fit_text(
        pdf,
        zip_text,
        91.32 + x_offset,
        y["zip"],
        BODY_FONT,
        ZIP_PHONE_FONT_SIZE,
        PHONE_MAX_WIDTH,
        min_size=8.0,
    )

    address_lines = _address_lines(row, warnings)
    for line_index, line in enumerate(address_lines[:2]):
        _draw_fit_text(
            pdf,
            line,
            91.44 + x_offset,
            y["address1" if line_index == 0 else "address2"],
            BODY_FONT,
            ADDRESS_FONT_SIZE,
            ADDRESS_MAX_WIDTH,
            min_size=8.25,
        )

    _draw_fit_text(
        pdf,
        _recipient_name(row),
        92.04 + x_offset,
        y["name"],
        BODY_FONT,
        NAME_FONT_SIZE,
        NAME_MAX_WIDTH,
        min_size=10.0,
    )

    phone = _phone_text(row.get("TEL", ""))
    if phone:
        _draw_fit_text(
            pdf,
            phone,
            91.32 + x_offset,
            y["phone"],
            BODY_FONT,
            ZIP_PHONE_FONT_SIZE,
            PHONE_MAX_WIDTH,
            min_size=8.0,
        )


def _draw_static_labels(pdf: canvas.Canvas, *, x_offset: float, y: dict[str, float]) -> None:
    pdf.setFont(LABEL_FONT, LABEL_FONT_SIZE)
    pdf.drawString(54.24 + x_offset, y["addr_jp"], "おところ：")
    pdf.drawString(58.68 + x_offset, y["addr_en"], "Address")
    pdf.drawString(51.36 + x_offset, y["name_jp"], "おなまえ：")
    pdf.drawString(51.36 + x_offset, y["name_en"], "Name")
    pdf.drawString(51.36 + x_offset, y["phone_jp"], "電話番号：")
    pdf.drawString(51.36 + x_offset, y["phone_en1"], "Telephone")
    pdf.drawString(51.36 + x_offset, y["phone_en2"], "Number")


def _draw_fit_text(
    pdf: canvas.Canvas,
    text: str,
    x: float,
    y: float,
    font_name: str,
    font_size: float,
    max_width: float,
    *,
    min_size: float,
) -> None:
    clean_text = _clean_text(text)
    if not clean_text:
        return
    size = _fit_font_size(clean_text, font_name, font_size, max_width, min_size=min_size)
    pdf.setFont(font_name, size)
    pdf.drawString(x, y, clean_text)


def _fit_font_size(
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
    *,
    min_size: float,
) -> float:
    size = font_size
    while size > min_size and pdfmetrics.stringWidth(text, font_name, size) > max_width:
        size = round(size - 0.25, 2)
    return max(size, min_size)


def _address_lines(row: dict[str, str], warnings: list[str]) -> list[str]:
    address1 = _clean_text(row.get("住所1", ""))
    address2 = _clean_text(row.get("住所2", ""))
    lines = [line for line in (address1, address2) if line]
    if len(lines) == 2:
        return lines
    if len(lines) == 1 and pdfmetrics.stringWidth(lines[0], BODY_FONT, ADDRESS_FONT_SIZE) <= ADDRESS_MAX_WIDTH:
        return lines[:2]

    combined = "".join(lines)
    wrapped = _wrap_to_width(combined, BODY_FONT, ADDRESS_FONT_SIZE, ADDRESS_MAX_WIDTH, max_lines=2)
    if len(wrapped) > 2:
        warnings.append(f"レターパック住所が2行に収まらないため末尾を2行目へ結合しました: {row.get('宛名2（氏名）', '')}")
    return wrapped[:2]


def _wrap_to_width(
    text: str,
    font_name: str,
    font_size: float,
    max_width: float,
    *,
    max_lines: int,
) -> list[str]:
    clean_text = _clean_text(text)
    if not clean_text:
        return []

    lines: list[str] = []
    current = ""
    for char in clean_text:
        candidate = current + char
        if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)

    if len(lines) <= max_lines:
        return lines
    return lines[: max_lines - 1] + ["".join(lines[max_lines - 1 :])]


def _recipient_name(row: dict[str, str]) -> str:
    company = _clean_text(row.get("宛名1（社名など）", ""))
    person = _clean_text(row.get("宛名2（氏名）", ""))
    name = " ".join(part for part in (company, person) if part)
    if name.endswith("様") or name.endswith("御中"):
        return name
    return f"{name} 様" if name else ""


def _zip_text(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    return text if text.startswith("〒") else f"〒{text}"


def _phone_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip()
    digits = re.sub(r"\D", "", text)
    return digits or text


def _clean_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _has_printable_name(row: dict[str, str]) -> bool:
    return bool(_clean_text(row.get("宛名1（社名など）", "")) or _clean_text(row.get("宛名2（氏名）", "")))


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    return {header: _clean_text(row.get(header, "")) for header in LETTERPACK_ADDRESS_HEADERS}


def _read_letterpack_csv(path: Path) -> list[dict[str, str]]:
    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="") as fp:
                return [dict(row) for row in csv.DictReader(fp)]
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, f"CSVを読み込めません: {path}")


def _register_fonts() -> None:
    _register_font(
        LABEL_FONT,
        (
            Path("C:/Windows/Fonts/HGRSGU.TTC"),
            Path("C:/Windows/Fonts/BIZ-UDGothicR.ttc"),
            Path("C:/Windows/Fonts/msgothic.ttc"),
        ),
    )
    _register_font(
        BODY_FONT,
        (
            Path("C:/Windows/Fonts/HGRSMP.TTF"),
            Path("C:/Windows/Fonts/HGRGM.TTC"),
            Path("C:/Windows/Fonts/meiryo.ttc"),
        ),
    )


def _register_font(name: str, candidates: Iterable[Path]) -> None:
    if name in pdfmetrics.getRegisteredFontNames():
        return
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(name, str(candidate)))
            return
        except Exception as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"レターパックPDF用フォントを登録できませんでした: {name}") from last_error
    raise FileNotFoundError(f"レターパックPDF用フォントが見つかりません: {name}")


def _append_audit(kind: str, result: LetterPackLabelPdfResult) -> None:
    LETTERPACK_PDF_AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "logged_at": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "result": _json_safe(result),
    }
    with LETTERPACK_PDF_AUDIT_LOG_PATH.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _json_safe(value):
    if hasattr(value, "__dataclass_fields__"):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for key, item in value.items():
            if key == "preview_rows":
                sanitized["preview_rows_count"] = len(item) if isinstance(item, (list, tuple)) else 0
                continue
            sanitized[str(key)] = _json_safe(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value
