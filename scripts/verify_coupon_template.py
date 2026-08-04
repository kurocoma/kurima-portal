"""クーポン集計ブックのデータソースが「動的解決」になっているかを検証する。

背景: テンプレート `【テンプレート】○○月クーポン利用 .xlsm` は Power Query の
参照先が特定ユーザーの絶対パス（C:\\Users\\kamiz\\...）で焼き付いており、
別PCで開くと `[DataSource.NotFound]` になっていた。修正後は

  1. Power Query の M 式（customXml の DataMashup 内 Formulas/Section1.m）が
     ドライブレター直書きではなく、ブック内セル（setup[filePath] /
     setup_fileName[fileName]）を参照していること
  2. 複製ブック側の参照セル（A2 / A9）は、そのPCで解決した実パス・実ファイル名が
     **数式なしの静的な文字列**として入っていること（開いた月・マクロ有効可否で
     値が動かないこと）

を満たす。この2点を機械的に確認する。

使い方（uv 経由・引数なしなら実フォルダを自動検出して全ブックを検査）:

    uv run python scripts/verify_coupon_template.py
    uv run python scripts/verify_coupon_template.py "D:\\path\\7月クーポン利用 .xlsm"
    uv run python scripts/verify_coupon_template.py --all-months   # 過去月のブックも検査
    uv run python scripts/verify_coupon_template.py --backup-dir   # 退避済み原本も検査

終了コード 0=全て合格 / 1=不合格あり。
"""

from __future__ import annotations

import argparse
import base64
import io
import re
import struct
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services.coupon_statements import (  # noqa: E402
    _CALC_CHAIN_NAME,
    _FILE_NAME_CELL,
    _FILE_PATH_CELL,
    _SHEET_XML_NAME,
    find_existing_workbook,
    read_cell_text,
    resolve_period,
)
from portal_app.services.excel_io import excel_read_snapshot  # noqa: E402
from portal_app.services.paths import find_coupon_paths  # noqa: E402


# 原本バックアップの永続退避先（tmp/ は掃除対象なので置かない）。
BACKUP_DIR = Path(__file__).resolve().parents[1] / "data" / "coupon_statements" / "template_backup"

# ドライブレター直書きの絶対パス（= 他PCで壊れる書き方）。
_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:\\\\?[^\"']{0,200}")
# 動的解決の証拠（ブック内セル参照）。
_DYNAMIC_MARKERS = ("Excel.CurrentWorkbook", "filePath", "fileName")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def extract_mashup_section(book: zipfile.ZipFile) -> str | None:
    """customXml の DataMashup（base64 + zip）から M 式本体を取り出す。"""

    for name in book.namelist():
        if not name.startswith("customXml/item"):
            continue
        try:
            root = ET.fromstring(book.read(name))
        except ET.ParseError:
            continue
        nodes = [node for node in root.iter() if _local_name(node.tag) == "DataMashup"]
        if not nodes or not nodes[0].text:
            continue
        blob = base64.b64decode("".join(nodes[0].text.split()), validate=True)
        if len(blob) < 8:
            continue
        _version, package_len = struct.unpack_from("<II", blob, 0)
        package = blob[8 : 8 + package_len]
        try:
            with zipfile.ZipFile(io.BytesIO(package)) as inner:
                return inner.read("Formulas/Section1.m").decode("utf-8", errors="replace")
        except (zipfile.BadZipFile, KeyError):
            continue
    return None


def _cell_has_formula(sheet_xml: str, cell_ref: str) -> bool:
    match = re.search(
        rf'<c r="{re.escape(cell_ref)}"(?:\s[^>]*?)?(?:/>|>(.*?)</c>)', sheet_xml, re.S
    )
    return bool(match and match.group(1) and "<f" in match.group(1))


def verify_workbook(path: Path, *, is_template: bool) -> tuple[bool, list[str]]:
    """1ブックを検査して (合格か, 表示行) を返す。"""

    lines: list[str] = []
    ok = True
    with excel_read_snapshot(path) as snapshot:
        with zipfile.ZipFile(snapshot) as book:
            names = book.namelist()
            sheet_xml = book.read(_SHEET_XML_NAME).decode("utf-8")
            section = extract_mashup_section(book)

    # 1. Power Query の M 式
    if section is None:
        ok = False
        lines.append("  [NG] Power Query の DataMashup を取り出せませんでした")
    else:
        hardcoded = sorted(set(_ABSOLUTE_PATH_RE.findall(section)))
        if hardcoded:
            ok = False
            lines.append("  [NG] M式にドライブレター直書きの絶対パスがあります:")
            lines.extend(f"        {item}" for item in hardcoded[:5])
        else:
            lines.append("  [OK] M式に絶対パスの直書きなし")
        found = [marker for marker in _DYNAMIC_MARKERS if marker in section]
        if "Excel.CurrentWorkbook" in found:
            lines.append(f"  [OK] M式はブック内セル参照で解決（{'/'.join(found)}）")
        else:
            ok = False
            lines.append("  [NG] M式に Excel.CurrentWorkbook 参照が見つかりません")

    # 2. 参照セル（A2 = filePath / A9 = fileName）
    file_path = read_cell_text(sheet_xml, _FILE_PATH_CELL)
    file_name = read_cell_text(sheet_xml, _FILE_NAME_CELL)
    lines.append(f"  filePath({_FILE_PATH_CELL}) = {file_path or '(空)'}")
    lines.append(f"  fileName({_FILE_NAME_CELL}) = {file_name or '(空)'}")

    has_formula = any(
        _cell_has_formula(sheet_xml, ref) for ref in (_FILE_PATH_CELL, _FILE_NAME_CELL)
    )
    if is_template:
        lines.append(
            "  [OK] テンプレートなので数式は残っていて構いません"
            f"（現在: {'数式あり' if has_formula else '静的値'}）"
        )
    else:
        if has_formula:
            ok = False
            lines.append(
                "  [NG] 複製ブックの A2/A9 に数式が残っています"
                "（開いた月に再計算されて参照先がずれます）"
            )
        else:
            lines.append("  [OK] 複製ブックの A2/A9 は静的な文字列")
        if not file_path or not file_name:
            ok = False
            lines.append("  [NG] 参照セルが空です（複製時の書き込みに失敗しています）")
        if _CALC_CHAIN_NAME in names:
            lines.append(
                "  [警告] calcChain.xml が残っています"
                "（数式削除と食い違うと Excel が修復を促す場合があります）"
            )
        else:
            lines.append("  [OK] calcChain.xml は除去済み（Excelが再生成します）")
    return ok, lines


def collect_targets(
    explicit: list[str], *, include_backups: bool, all_months: bool
) -> list[tuple[Path, bool]]:
    """検査対象 [(パス, テンプレートか)] を集める。

    既定はテンプレートと「今回の対象月（前月）」のブックだけ。過去の月のブックは
    このカード以前に手作業で作られたものがあり、数式が残っていて当然なので
    既定では見ない（`--all-months` で全月を検査できる）。
    """

    targets: list[tuple[Path, bool]] = []
    if explicit:
        for raw in explicit:
            path = Path(raw)
            targets.append((path, "テンプレート" in path.name))
        return targets

    paths = find_coupon_paths()
    if paths.template_path.is_file():
        targets.append((paths.template_path, True))
    months = range(1, 13) if all_months else (resolve_period().month,)
    for month in months:
        workbook = find_existing_workbook(paths.root, month)
        if workbook is not None:
            targets.append((workbook, False))
    if include_backups and BACKUP_DIR.is_dir():
        for backup in sorted(BACKUP_DIR.glob("*.xlsm")):
            targets.append((backup, True))
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbooks", nargs="*", help="検査する xlsm（省略時は自動検出）")
    parser.add_argument(
        "--backup-dir",
        action="store_true",
        help=f"退避済み原本（{BACKUP_DIR}）も検査対象に含める",
    )
    parser.add_argument(
        "--all-months",
        action="store_true",
        help="過去月のブックもすべて検査する（既定は前月分のみ）",
    )
    args = parser.parse_args()

    period = resolve_period()
    print(f"対象期間: {period.label}（想定CSV: {period.csv_name}）")

    try:
        targets = collect_targets(
            args.workbooks, include_backups=args.backup_dir, all_months=args.all_months
        )
    except FileNotFoundError as exc:
        print(f"[NG] {exc}")
        return 1
    if not targets:
        print("[NG] 検査対象のブックが見つかりませんでした。")
        return 1

    failed = 0
    for path, is_template in targets:
        kind = "テンプレート" if is_template else "複製ブック"
        print(f"\n== {kind}: {path}")
        if not path.is_file():
            print("  [NG] ファイルがありません")
            failed += 1
            continue
        try:
            ok, lines = verify_workbook(path, is_template=is_template)
        except Exception as exc:  # ロック・破損などは落とさず不合格として報告する
            print(f"  [NG] 検査できませんでした: {exc}")
            failed += 1
            continue
        for line in lines:
            print(line)
        if not ok:
            failed += 1

    print(f"\n結果: {len(targets) - failed} / {len(targets)} 合格")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
