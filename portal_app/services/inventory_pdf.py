from __future__ import annotations

from io import BytesIO
from typing import Literal
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from portal_app.services.inventory import InventoryResult

JP_FONT = "HeiseiKakuGo-W5"
InventoryPdfKind = Literal["all", "normal", "choice", "combined"]
PDF_KIND_TITLES: dict[InventoryPdfKind, str] = {
    "all": "在庫明細確認",
    "normal": "在庫明細確認 - 通常商品",
    "choice": "在庫明細確認 - 選べるセット",
    # 2026-07-20 依頼2: 通常商品とセット内訳をJANコードで対応付けた数量合算表
    "combined": "在庫明細確認 - 合算（通常＋選べるセット）",
}


def inventory_result_to_pdf(result: InventoryResult, kind: InventoryPdfKind = "all") -> bytes:
    if kind not in PDF_KIND_TITLES:
        raise ValueError(f"unknown inventory PDF kind: {kind}")
    _register_fonts()
    title = PDF_KIND_TITLES[kind]

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=title,
    )
    styles = _styles()
    story = []

    if kind in {"all", "normal"}:
        story.append(
            _data_table(
                headers=("商品コード", "商品名", "受注数", "引当数"),
                rows=result.normal_rows,
                col_widths=(80, doc.width - 240, 80, 80),
                numeric_indexes={2, 3},
                styles=styles,
            )
        )

    if kind == "all":
        story.append(Spacer(1, 10))

    if kind in {"all", "choice"}:
        story.append(
            _data_table(
                headers=("商品名", "発注数量", "備考"),
                rows=result.choice_rows,
                col_widths=(doc.width - 220, 80, 140),
                numeric_indexes={1},
                styles=styles,
            )
        )

    if kind == "combined":
        story.append(
            _data_table(
                headers=("商品コード", "商品名", "必要数", "備考"),
                rows=result.combined_rows,
                col_widths=(110, doc.width - 110 - 80 - 120, 80, 120),
                numeric_indexes={2},
                styles=styles,
            )
        )

    doc.build(story, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buffer.getvalue()


def inventory_normal_result_to_pdf(result: InventoryResult) -> bytes:
    return inventory_result_to_pdf(result, "normal")


def inventory_choice_result_to_pdf(result: InventoryResult) -> bytes:
    return inventory_result_to_pdf(result, "choice")


TAKAESU_PDF_HEADERS = ("JANコード", "仕入先CD", "商品名", "発注数", "受注数", "備考")
TAKAESU_PDF_NUMERIC = {"発注数", "受注数"}


def takaesu_order_sheet_to_pdf(rows: list[dict[str, object]]) -> bytes:
    """高江洲発注書（JANコード/仕入先CD/商品名/発注数/受注数/備考）をPDF化する。

    rows は preview_takaesu_order_sheet 等が返す dict レコード列（列順に依存せず
    ヘッダーのキーで参照する）。
    """
    _register_fonts()
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title="高江洲発注書",
    )
    styles = _styles()
    data: list[list[object]] = [
        [Paragraph(_escaped(header), styles["header"]) for header in TAKAESU_PDF_HEADERS]
    ]
    row_list = list(rows or [])
    if row_list:
        for row in row_list:
            data.append(
                [
                    Paragraph(
                        _escaped(row.get(header, "")),
                        styles["cell_right"] if header in TAKAESU_PDF_NUMERIC else styles["cell"],
                    )
                    for header in TAKAESU_PDF_HEADERS
                ]
            )
    else:
        data.append([Paragraph("対象データなし", styles["cell"])] + [""] * (len(TAKAESU_PDF_HEADERS) - 1))

    col_widths = (90, 70, doc.width - 90 - 70 - 70 - 70 - 150, 70, 70, 150)
    style_commands: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e79")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ALIGN", (3, 1), (4, -1), "RIGHT"),
    ]
    if not row_list:
        style_commands.append(("SPAN", (0, 1), (-1, 1)))

    table = Table(data, colWidths=col_widths, repeatRows=1, splitByRow=True)
    table.setStyle(TableStyle(style_commands))
    doc.build([table], onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    return buffer.getvalue()


def _register_fonts() -> None:
    if JP_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(JP_FONT))


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "header": ParagraphStyle(
            "InventoryPdfHeader",
            fontName=JP_FONT,
            fontSize=8,
            leading=10,
            textColor=colors.white,
            wordWrap="CJK",
        ),
        "cell": ParagraphStyle(
            "InventoryPdfCell",
            fontName=JP_FONT,
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#111827"),
            wordWrap="CJK",
        ),
        "cell_right": ParagraphStyle(
            "InventoryPdfCellRight",
            fontName=JP_FONT,
            fontSize=8,
            leading=10,
            textColor=colors.HexColor("#111827"),
            alignment=TA_RIGHT,
            wordWrap="CJK",
        ),
    }


def _data_table(
    *,
    headers: tuple[str, ...],
    rows: list[dict[str, object]],
    col_widths: tuple[float, ...],
    numeric_indexes: set[int],
    styles: dict[str, ParagraphStyle],
) -> Table:
    data: list[list[object]] = [
        [Paragraph(_escaped(header), styles["header"]) for header in headers]
    ]
    if rows:
        for row in rows:
            values = list(row.values())
            data.append(
                [
                    Paragraph(
                        _escaped(values[index] if index < len(values) else ""),
                        styles["cell_right"] if index in numeric_indexes else styles["cell"],
                    )
                    for index in range(len(headers))
                ]
            )
    else:
        data.append([Paragraph("対象データなし", styles["cell"])] + [""] * (len(headers) - 1))

    style_commands: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e79")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
        ("BOX", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for index in numeric_indexes:
        style_commands.append(("ALIGN", (index, 1), (index, -1), "RIGHT"))
    if not rows:
        style_commands.append(("SPAN", (0, 1), (-1, 1)))

    table = Table(data, colWidths=col_widths, repeatRows=1, splitByRow=True)
    table.setStyle(TableStyle(style_commands))
    return table


def _draw_footer(canvas, doc) -> None:
    _register_fonts()
    canvas.saveState()
    canvas.setFont(JP_FONT, 8)
    canvas.setFillColor(colors.HexColor("#6b7280"))
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 8 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _escaped(value: object) -> str:
    return escape(str(value).replace("\r\n", "\n").replace("\r", "\n")).replace("\n", "<br/>")
