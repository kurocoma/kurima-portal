from __future__ import annotations

import json
import math
import csv
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd

from portal_app.services.inventory import read_master_tables, read_order_csv
from portal_app.services.master_cache import cached_by_mtimes
from portal_app.services.next_engine_downloader import APP_ROOT, download_order_details_to_directory_sync
from portal_app.services.paths import find_portal_paths
from portal_app.services.yamato_conversion import read_excel_table


FLOW_ID = "404af78e-82ee-4318-a6f5-2e5c44d27157"
FLOW_NAME = "高江洲発注明細_2"
AUDIT_LOG_DIR = APP_ROOT / "logs" / "takaesu_orders"
AUDIT_LOG_PATH = AUDIT_LOG_DIR / "takaesu_order_download_audit.jsonl"
TAKAESU_SOURCE_DIR_PARTS = ("ネクストエンジン", "発注関連", "受注明細一覧-高江洲")
TAKAESU_TOOL_DIR_PARTS = ("ネクストエンジン", "発注関連")
TAKAESU_TOOL_WORKBOOK = "在庫明細確認_高江洲_ver1.xlsm"
TAKAESU_OUTPUT_FILE = "高江洲発注書.csv"
TAKAESU_OUTPUT_HEADERS = ("JANコード", "仕入先CD", "商品名", "発注数", "受注数", "備考")
TAKAESU_ORDER_STATUS_OPTIONS = (
    "1 : 受注メール取込済",
    "2 : 起票済(CSV/手入力)",
    "20 : 納品書印刷待ち",
    "30 : 納品書印刷中",
    "40 : 納品書印刷済",
)
TAKAESU_PAYMENT_OPTIONS = ("2 : 入金済み",)


@dataclass(frozen=True)
class TakaesuOrderStep:
    subflow: str
    target: str
    status: str
    notes: tuple[str, ...]


@dataclass(frozen=True)
class TakaesuOrderDownloadResult:
    captured_at: datetime
    executed: bool
    flow_id: str
    flow_name: str
    target_date: str | None
    order_numbers: tuple[str, ...]
    source_sample_input: Path | None
    expected_contract: Path | None
    steps: tuple[TakaesuOrderStep, ...]
    downloaded_file: Path | None
    source_filename: str | None
    output_rows: int | None
    order_sheet: "TakaesuOrderSheetResult | None"
    skipped_reason: str | None
    audit_path: Path | None


@dataclass(frozen=True)
class TakaesuOrderSheetResult:
    source_csv: Path
    master_book: Path
    order_workbook: Path
    output_csv: Path | None
    source_rows: int
    normal_rows: int
    choice_rows: int
    output_rows: int
    warnings: tuple[str, ...]
    preview_rows: tuple[dict[str, object], ...]
    audit_path: Path | None


@dataclass(frozen=True)
class TakaesuOrderWorkflowResult:
    captured_at: datetime
    executed: bool
    wrote_order_sheet: bool
    flow_id: str
    flow_name: str
    source_sample_input: Path | None
    expected_contract: Path | None
    steps: tuple[TakaesuOrderStep, ...]
    download: TakaesuOrderDownloadResult | None
    order_sheet: TakaesuOrderSheetResult | None
    skipped_reason: str | None
    audit_path: Path | None


def download_takaesu_order_details_sync(
    *,
    execute: bool,
    target_date: str | None = None,
    order_numbers: Iterable[str] = (),
    sample_input: str | Path | None = None,
    expected_contract: str | Path | None = None,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    write_audit: bool = True,
) -> TakaesuOrderDownloadResult:
    """Plan or execute the Takaesu order-detail download replacement.

    This maps the PAD ne検索ダウンロード subflow. It keeps the PAD search
    conditions but writes to an explicit file path instead of driving Save As.
    """

    sample_path = Path(sample_input) if sample_input else None
    expected_path = Path(expected_contract) if expected_contract else None
    records = tuple(_clean_records(order_numbers)) or _records_from_sample(sample_path)
    skipped_reason = None
    executed = False
    downloaded_file = None
    source_filename = None
    output_rows = None
    order_sheet = None
    if execute:
        paths = find_portal_paths()
        source_dir = paths.portal_root.joinpath(*TAKAESU_SOURCE_DIR_PARTS)
        downloaded = download_order_details_to_directory_sync(
            destination_dir=source_dir,
            order_status_options=TAKAESU_ORDER_STATUS_OPTIONS,
            payment_options=TAKAESU_PAYMENT_OPTIONS,
            data_file_prefix="data",
            headless=headless,
            slow_mo_ms=slow_mo_ms,
        )
        downloaded_file = downloaded.downloaded_file
        source_filename = downloaded.source_filename
        output_rows = _count_csv_rows(downloaded_file)
        order_sheet = preview_takaesu_order_sheet(source_csv=downloaded_file, write_audit=False)
        executed = True

    result = TakaesuOrderDownloadResult(
        captured_at=datetime.now(),
        executed=executed,
        flow_id=FLOW_ID,
        flow_name=FLOW_NAME,
        target_date=target_date or _target_date_from_sample(sample_path),
        order_numbers=records,
        source_sample_input=sample_path,
        expected_contract=expected_path,
        steps=_planned_steps(),
        downloaded_file=downloaded_file,
        source_filename=source_filename,
        output_rows=output_rows,
        order_sheet=order_sheet,
        skipped_reason=skipped_reason,
        audit_path=AUDIT_LOG_PATH if write_audit else None,
    )
    if write_audit:
        _append_audit(result)
    return result


def prepare_takaesu_order_workflow_sync(
    *,
    dry_run: bool,
    execute_download: bool = False,
    write_order_sheet: bool = False,
    source_csv: Path | None = None,
    output_csv: Path | None = None,
    sample_input: str | Path | None = None,
    expected_contract: str | Path | None = None,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    preview_limit: int = 50,
    write_audit: bool = True,
) -> TakaesuOrderWorkflowResult:
    """Run the PAD Main-equivalent Takaesu workflow boundary."""

    sample_path = Path(sample_input) if sample_input else None
    expected_path = Path(expected_contract) if expected_contract else None
    download_result: TakaesuOrderDownloadResult | None = None
    order_sheet: TakaesuOrderSheetResult | None = None
    skipped_reason = "dry_run" if dry_run else None

    if not dry_run:
        source = source_csv
        if execute_download:
            download_result = download_takaesu_order_details_sync(
                execute=True,
                sample_input=sample_path,
                expected_contract=expected_path,
                headless=headless,
                slow_mo_ms=slow_mo_ms,
                write_audit=False,
            )
            source = download_result.downloaded_file

        if write_order_sheet:
            output_csv = output_csv or default_takaesu_order_sheet_csv_path()
            order_sheet = create_takaesu_order_sheet_csv(
                source_csv=source,
                output_csv=output_csv,
                preview_limit=preview_limit,
                write_audit=False,
            )
        else:
            order_sheet = preview_takaesu_order_sheet(
                source_csv=source,
                preview_limit=preview_limit,
                write_audit=False,
            )

    result = TakaesuOrderWorkflowResult(
        captured_at=datetime.now(),
        executed=not dry_run and (execute_download or write_order_sheet),
        wrote_order_sheet=bool(order_sheet and order_sheet.output_csv),
        flow_id=FLOW_ID,
        flow_name=FLOW_NAME,
        source_sample_input=sample_path,
        expected_contract=expected_path,
        steps=_planned_steps(),
        download=download_result,
        order_sheet=order_sheet,
        skipped_reason=skipped_reason,
        audit_path=AUDIT_LOG_PATH if write_audit else None,
    )
    if write_audit:
        _append_audit_payload("takaesu_order_workflow", result)
    return result


def default_takaesu_order_sheet_csv_path() -> Path:
    paths = find_portal_paths()
    return paths.portal_root.joinpath(*TAKAESU_TOOL_DIR_PARTS) / TAKAESU_OUTPUT_FILE


def preview_takaesu_order_sheet(
    *,
    source_csv: Path | None = None,
    preview_limit: int = 50,
    write_audit: bool = False,
) -> TakaesuOrderSheetResult:
    """読み取り専用のプレビュー集計。

    書き込み（CSV出力・監査ログ）を伴わない場合のみ、全入力ファイル
    （受注明細CSV・商品マスタ・高江洲発注書ブック）の mtime を合成キーにした
    キャッシュを使う。いずれかのファイルが更新されると再計算される。
    新しい受注明細 CSV が追加された場合も、最新CSVの解決（_latest_data_csv）を
    毎回行うためキーのパス自体が変わり、キャッシュミスとして再計算される。
    書き込みを伴う実行経路（write_audit=True や create_takaesu_order_sheet_csv）は
    キャッシュしない。
    """
    if write_audit:
        return _build_takaesu_order_sheet(
            source_csv=source_csv,
            output_csv=None,
            preview_limit=preview_limit,
            write_audit=True,
        )

    paths = find_portal_paths()
    source = source_csv or _latest_data_csv(paths.portal_root.joinpath(*TAKAESU_SOURCE_DIR_PARTS))
    order_workbook = paths.portal_root.joinpath(*TAKAESU_TOOL_DIR_PARTS) / TAKAESU_TOOL_WORKBOOK
    return cached_by_mtimes(
        (source, paths.master_book, order_workbook),
        key=f"takaesu_preview:{source}:{preview_limit}",
        loader=lambda: _build_takaesu_order_sheet(
            source_csv=source,
            output_csv=None,
            preview_limit=preview_limit,
            write_audit=False,
        ),
    )


def create_takaesu_order_sheet_csv(
    *,
    source_csv: Path | None = None,
    output_csv: Path | None = None,
    preview_limit: int = 50,
    write_audit: bool = True,
) -> TakaesuOrderSheetResult:
    return _build_takaesu_order_sheet(
        source_csv=source_csv,
        output_csv=output_csv,
        preview_limit=preview_limit,
        write_audit=write_audit,
    )


def _planned_steps() -> tuple[TakaesuOrderStep, ...]:
    return (
        TakaesuOrderStep(
            subflow="Main",
            target="portal_tool/portal_app/services/takaesu_orders.py",
            status="mapped",
            notes=("prepare_takaesu_order_workflow_sync orchestrates downloadform, ne検索ダウンロード, and excel開く.",),
        ),
        TakaesuOrderStep(
            subflow="downloadform",
            target="Playwright/browser download save_as or explicit source_csv path",
            status="mapped",
            notes=("PAD save dialog is replaced by explicit file destinations.",),
        ),
        TakaesuOrderStep(
            subflow="ne検索ダウンロード",
            target="Next Engine Playwright search/download",
            status="mapped",
            notes=(
                "Uses order statuses 1/2/20/30/40 and payment status 入金済み.",
                "Downloads to the Takaesu order-detail folder with an explicit dataYYMMDDHHMM.csv path.",
                "Keeps existing files instead of emptying the folder so verification evidence remains available.",
            ),
        ),
        TakaesuOrderStep(
            subflow="excel開く",
            target="preview_takaesu_order_sheet/create_takaesu_order_sheet_csv",
            status="mapped",
            notes=("Replaces 在庫明細確認_高江洲_ver1.xlsm Power Query and formula column.",),
        ),
    )


def _build_takaesu_order_sheet(
    *,
    source_csv: Path | None,
    output_csv: Path | None,
    preview_limit: int,
    write_audit: bool,
) -> TakaesuOrderSheetResult:
    paths = find_portal_paths()
    portal_root = paths.portal_root
    source_dir = portal_root.joinpath(*TAKAESU_SOURCE_DIR_PARTS)
    source = source_csv or _latest_data_csv(source_dir)
    order_workbook = portal_root.joinpath(*TAKAESU_TOOL_DIR_PARTS) / TAKAESU_TOOL_WORKBOOK
    output_path = output_csv
    if output_path is None and write_audit:
        output_path = portal_root.joinpath(*TAKAESU_TOOL_DIR_PARTS) / TAKAESU_OUTPUT_FILE

    warnings: list[str] = []
    orders = read_order_csv(source)
    masters = read_master_tables(paths.master_book)
    product_list = _build_product_list(masters.product_master)
    order_sequence = _read_order_sequence(order_workbook)

    normal = _build_takaesu_normal_rows(orders, masters.choice_master, product_list)
    choice = _build_takaesu_choice_rows(orders, masters.choice_master, order_sequence, warnings)
    order_sheet = _build_takaesu_order_sheet_rows(normal, choice, product_list, order_sequence)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        order_sheet.to_csv(output_path, index=False, encoding="cp932", columns=list(TAKAESU_OUTPUT_HEADERS))

    preview_rows = tuple(_records(order_sheet.head(preview_limit)))
    result = TakaesuOrderSheetResult(
        source_csv=source,
        master_book=paths.master_book,
        order_workbook=order_workbook,
        output_csv=output_path,
        source_rows=len(orders),
        normal_rows=len(normal),
        choice_rows=len(choice),
        output_rows=len(order_sheet),
        warnings=tuple(warnings),
        preview_rows=preview_rows,
        audit_path=AUDIT_LOG_PATH if write_audit else None,
    )
    if write_audit:
        _append_audit_payload("takaesu_order_sheet", result)
    return result


def _latest_data_csv(directory: Path) -> Path:
    files = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.name.lower().startswith("data") and path.suffix.lower() == ".csv"
    ]
    if not files:
        raise FileNotFoundError(f"data で始まる高江洲受注明細 CSV が見つかりません: {directory}")
    return max(files, key=lambda path: path.stat().st_mtime)


def _count_csv_rows(path: Path) -> int:
    for encoding in ("cp932", "utf-8-sig", "utf-8"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                return sum(1 for _ in csv.DictReader(handle))
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("csv", b"", 0, 1, f"unsupported CSV encoding: {path}")


def _build_product_list(product_master: pd.DataFrame) -> pd.DataFrame:
    product_list = product_master.rename(
        columns={"JANコード": "jan_code", "NEコード": "商品コード"}
    ).copy()
    required = ["仕入先CD", "商品コード", "jan_code", "商品名"]
    missing = [column for column in required if column not in product_list.columns]
    if missing:
        raise ValueError("商品マスタの列が不足しています: " + ", ".join(missing))
    return product_list[required].drop_duplicates(subset=["jan_code"]).copy()


def _read_order_sequence(order_workbook: Path) -> pd.DataFrame:
    rows = read_excel_table(order_workbook, "並び順")
    frame = pd.DataFrame(rows).fillna("")
    required = ["NEコード", "仕入先CD", "JANコード", "商品名", "並び順", "備考"]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError("高江洲 並び順 テーブルの列が不足しています: " + ", ".join(missing))
    frame["並び順"] = pd.to_numeric(frame["並び順"], errors="coerce")
    return frame[required].copy()


def _base_order_rows(orders: pd.DataFrame) -> pd.DataFrame:
    work = orders[~orders["作業者欄"].astype(str).str.contains("発注", na=False)].copy()
    return work.drop_duplicates(subset=["伝票番号", "明細行"]).reset_index(drop=True)


def _build_takaesu_normal_rows(
    orders: pd.DataFrame,
    choice_master: pd.DataFrame,
    product_list: pd.DataFrame,
) -> pd.DataFrame:
    work = _base_order_rows(orders)[["商品ｺｰﾄﾞ", "受注数", "引当数"]].copy()
    work = work[work["商品ｺｰﾄﾞ"].astype(str).str.contains("t002", na=False)]
    work = _left_anti(work, choice_master, ["商品ｺｰﾄﾞ"], ["NEコード"])
    work = work.merge(
        product_list[["商品コード", "jan_code"]],
        left_on="商品ｺｰﾄﾞ",
        right_on="商品コード",
        how="left",
    )
    grouped = (
        work.groupby(["商品ｺｰﾄﾞ", "jan_code"], dropna=False, as_index=False)["受注数"]
        .sum()
        .rename(columns={"jan_code": "JANコード"})
    )
    grouped["受注数"] = pd.to_numeric(grouped["受注数"], errors="coerce").fillna(0).astype(int)
    return grouped.loc[:, ["商品ｺｰﾄﾞ", "JANコード", "受注数"]].reset_index(drop=True)


def _build_takaesu_choice_rows(
    orders: pd.DataFrame,
    choice_master: pd.DataFrame,
    order_sequence: pd.DataFrame,
    warnings: list[str],
) -> pd.DataFrame:
    work = _base_order_rows(orders)[
        ["商品ｺｰﾄﾞ", "商品ｵﾌﾟｼｮﾝ", "受注数", "引当数", "作業者欄"]
    ].copy()
    choice_keys = choice_master[["NEコード"]].drop_duplicates(subset=["NEコード"])
    work = work.merge(choice_keys, left_on="商品ｺｰﾄﾞ", right_on="NEコード", how="inner")
    work = work.drop(columns=["NEコード", "作業者欄"])
    work = work[work["商品ｺｰﾄﾞ"].astype(str).str.contains("t002", na=False)]
    expanded = _expand_takaesu_options(work, warnings)
    if expanded.empty:
        return pd.DataFrame(columns=["JANコード", "受注数"])

    expanded["商品ｵﾌﾟｼｮﾝ.2"] = expanded["商品ｵﾌﾟｼｮﾝ.2"].replace({"ＮＯ.７": "NO.7"})
    expanded["テキスト範囲"] = expanded["商品ｺｰﾄﾞ"].astype(str).str[1:4]
    choice_lookup = choice_master[
        ["NEコード", "項目選択肢項目名", "項目選択肢", "JANコード", "数量"]
    ].copy()
    expanded = expanded.merge(
        choice_lookup,
        left_on=["商品ｺｰﾄﾞ", "商品ｵﾌﾟｼｮﾝ.1", "商品ｵﾌﾟｼｮﾝ.2"],
        right_on=["NEコード", "項目選択肢項目名", "項目選択肢"],
        how="left",
    )
    missing = expanded["JANコード"].fillna("").eq("").sum()
    if missing:
        warnings.append(f"高江洲 選べるセットの内訳マスタ未一致が {missing} 行あります。")

    expanded["数量"] = pd.to_numeric(expanded["数量"], errors="coerce").fillna(0).astype(int)
    expanded["受注数×内訳数量"] = expanded["受注数"] * expanded["数量"]
    grouped = (
        expanded.groupby(["JANコード", "テキスト範囲"], dropna=False, as_index=False)[
            "受注数×内訳数量"
        ]
        .sum()
        .rename(columns={"受注数×内訳数量": "受注数"})
    )
    grouped = grouped.merge(
        order_sequence[["JANコード", "並び順"]],
        on="JANコード",
        how="left",
    )
    grouped = grouped.sort_values(["並び順", "テキスト範囲"], na_position="last")
    grouped["受注数"] = pd.to_numeric(grouped["受注数"], errors="coerce").fillna(0).astype(int)
    return grouped.loc[:, ["JANコード", "受注数"]].reset_index(drop=True)


def _build_takaesu_order_sheet_rows(
    normal: pd.DataFrame,
    choice: pd.DataFrame,
    product_list: pd.DataFrame,
    order_sequence: pd.DataFrame,
) -> pd.DataFrame:
    combined = pd.concat([normal, choice], ignore_index=True, sort=False)
    if combined.empty:
        return pd.DataFrame(columns=list(TAKAESU_OUTPUT_HEADERS))

    grouped = combined.groupby("JANコード", dropna=False, as_index=False)["受注数"].sum()
    grouped = grouped.merge(
        product_list[["jan_code", "仕入先CD", "商品名"]],
        left_on="JANコード",
        right_on="jan_code",
        how="left",
    )
    grouped = grouped.merge(
        order_sequence[["JANコード", "並び順", "備考"]],
        on="JANコード",
        how="left",
    )
    grouped = grouped.sort_values("並び順", na_position="last")
    grouped["備考"] = grouped["備考"].fillna("")
    grouped["受注数"] = pd.to_numeric(grouped["受注数"], errors="coerce").fillna(0).astype(int)
    grouped["発注数"] = [
        quantity if not str(note).strip() else _ceil_to_unit(quantity, 6)
        for quantity, note in zip(grouped["受注数"], grouped["備考"], strict=False)
    ]
    grouped["仕入先CD"] = grouped["仕入先CD"].fillna("")
    grouped["商品名"] = grouped["商品名"].fillna("")
    return grouped.loc[:, list(TAKAESU_OUTPUT_HEADERS)].reset_index(drop=True)


def _expand_takaesu_options(work: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for row in work.to_dict(orient="records"):
        raw_options = str(row.get("商品ｵﾌﾟｼｮﾝ", "")).replace("　", "|").replace(" ", "|")
        for token in [part for part in raw_options.split("|") if part]:
            if ":" not in token:
                warnings.append(f"高江洲 商品オプションを分割できませんでした: {token}")
                continue
            name, value = token.split(":", 1)
            rows.append(
                {
                    "商品ｺｰﾄﾞ": row["商品ｺｰﾄﾞ"],
                    "商品ｵﾌﾟｼｮﾝ.1": name,
                    "商品ｵﾌﾟｼｮﾝ.2": value,
                    "受注数": row["受注数"],
                    "引当数": row["引当数"],
                }
            )
    return pd.DataFrame(rows)


def _left_anti(
    left: pd.DataFrame,
    right: pd.DataFrame,
    left_on: list[str],
    right_on: list[str],
) -> pd.DataFrame:
    keys = right[right_on].drop_duplicates().copy()
    keys["_matched"] = True
    merged = left.merge(keys, left_on=left_on, right_on=right_on, how="left")
    return merged[merged["_matched"].isna()].loc[:, left.columns].copy()


def _ceil_to_unit(value: int, unit: int) -> int:
    if value == 0:
        return 0
    return int(math.ceil(value / unit) * unit)


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


def _append_audit(result: TakaesuOrderDownloadResult) -> None:
    _append_audit_payload("takaesu_order_download", result)


def _append_audit_payload(kind: str, result) -> None:
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"logged_at": datetime.now().isoformat(), "kind": kind, "result": _json_safe(asdict(result))}
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


def _records(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    clean = frame.where(pd.notna(frame), "")
    return clean.to_dict(orient="records")
