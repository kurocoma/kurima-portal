from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Literal
import warnings

import pandas as pd

from portal_app.services.paths import PortalPaths, find_portal_paths, latest_order_csv
from portal_app.services.master_cache import cached_by_mtime

warnings.filterwarnings(
    "ignore",
    message=r"Print area cannot be set to Defined name: .*",
    category=UserWarning,
    module=r"openpyxl\.reader\.workbook",
)


ORDER_COLUMNS = [
    "受注日",
    "店舗",
    "購入者名",
    "受注番号",
    "伝票番号",
    "明細行",
    "商品ｺｰﾄﾞ",
    "商品名",
    "商品ｵﾌﾟｼｮﾝ",
    "取扱区分",
    "商品区分",
    "金額",
    "売単価",
    "受注数",
    "引当数",
    "送り先名",
    "送り先〒",
    "送り先住所",
    "作業者欄",
    "原価",
    "型番",
    "jan",
]


@dataclass(frozen=True)
class InventoryResult:
    generated_at: datetime
    paths: PortalPaths
    source_csv: Path
    source_rows: int
    normal_rows: list[dict[str, object]]
    choice_rows: list[dict[str, object]]
    warnings: list[str]
    # 2026-07-20 依頼2: 通常商品とセット内訳をJANコードで対応付けた数量合算表
    combined_rows: list[dict[str, object]] = field(default_factory=list)

    @property
    def normal_count(self) -> int:
        return len(self.normal_rows)

    @property
    def choice_count(self) -> int:
        return len(self.choice_rows)

    @property
    def combined_count(self) -> int:
        return len(self.combined_rows or [])


@dataclass(frozen=True)
class MasterTables:
    product_master: pd.DataFrame
    choice_master: pd.DataFrame
    shimanoya_master: pd.DataFrame


def analyze_latest_inventory() -> InventoryResult:
    paths = find_portal_paths()
    return analyze_inventory(paths=paths, source_csv=latest_order_csv(paths.order_csv_dir))


def analyze_inventory(paths: PortalPaths, source_csv: Path) -> InventoryResult:
    warnings: list[str] = []
    orders = read_order_csv(source_csv)
    masters = read_master_tables(paths.master_book)

    normal = build_normal_products(orders, masters)
    choice_detail = _build_choice_products_detail(orders, masters, warnings)
    choice = _choice_public_view(choice_detail)
    combined = build_combined_products(normal, choice_detail, masters)

    return InventoryResult(
        generated_at=datetime.now(),
        paths=paths,
        source_csv=source_csv,
        source_rows=len(orders),
        normal_rows=_records(normal),
        choice_rows=_records(choice),
        warnings=warnings,
        combined_rows=_records(combined),
    )


def read_order_csv(path: Path) -> pd.DataFrame:
    orders = pd.read_csv(
        path,
        encoding="cp932",
        dtype=str,
        keep_default_na=False,
        quotechar='"',
    )
    missing = [column for column in ORDER_COLUMNS if column not in orders.columns]
    if missing:
        raise ValueError(f"受注明細 CSV の列が不足しています: {', '.join(missing)}")

    for column in ("受注数", "引当数"):
        orders[column] = pd.to_numeric(orders[column], errors="coerce").fillna(0).astype(int)

    return orders


def read_master_tables(master_book: Path) -> MasterTables:
    cached = cached_by_mtime(
        master_book,
        "inventory_masters",
        lambda: _read_master_tables_impl(master_book),
    )
    return MasterTables(
        product_master=cached.product_master.copy(),
        choice_master=cached.choice_master.copy(),
        shimanoya_master=cached.shimanoya_master.copy(),
    )


def _read_master_tables_impl(master_book: Path) -> MasterTables:
    product_master = pd.read_excel(
        master_book,
        sheet_name="商品マスタ",
        dtype=str,
        engine="openpyxl",
    ).fillna("")
    choice_master = pd.read_excel(
        master_book,
        sheet_name="NEオプション一覧",
        dtype=str,
        engine="openpyxl",
    ).fillna("")
    shimanoya_master = pd.read_excel(
        master_book,
        sheet_name="しまのや商品コード一覧",
        dtype=str,
        engine="openpyxl",
    ).fillna("")

    product_master = _clean_columns(product_master)
    choice_master = _clean_columns(choice_master)
    shimanoya_master = _clean_columns(shimanoya_master)

    if "JANコード" in product_master.columns:
        product_master = product_master[product_master["JANコード"] != ""].drop_duplicates(
            subset=["JANコード"]
        )

    return MasterTables(
        product_master=product_master,
        choice_master=choice_master,
        shimanoya_master=shimanoya_master,
    )


def build_normal_products(orders: pd.DataFrame, masters: MasterTables) -> pd.DataFrame:
    work = _not_ordered_rows(orders)[["商品ｺｰﾄﾞ", "受注数", "引当数"]].copy()

    work = _left_anti(work, masters.choice_master, ["商品ｺｰﾄﾞ"], ["NEコード"])

    product_names = (
        masters.product_master[["NEコード", "商品名"]]
        .drop_duplicates(subset=["NEコード"])
        .copy()
    )
    work = work.merge(product_names, left_on="商品ｺｰﾄﾞ", right_on="NEコード", how="left")
    work["商品名"] = work["商品名"].fillna("")

    grouped = (
        work.groupby(["商品ｺｰﾄﾞ", "商品名"], dropna=False, as_index=False)[["受注数", "引当数"]]
        .sum()
        .loc[:, ["商品ｺｰﾄﾞ", "商品名", "受注数", "引当数"]]
    )
    grouped = grouped[grouped["商品ｺｰﾄﾞ"].astype(str) != ""]
    grouped = _left_anti(grouped, masters.shimanoya_master, ["商品ｺｰﾄﾞ"], ["商品コード"])
    return grouped.sort_values(["商品ｺｰﾄﾞ", "商品名"]).reset_index(drop=True)


def build_choice_products(
    orders: pd.DataFrame,
    masters: MasterTables,
    warnings: list[str],
) -> pd.DataFrame:
    return _choice_public_view(_build_choice_products_detail(orders, masters, warnings))


def _choice_public_view(choice_detail: pd.DataFrame) -> pd.DataFrame:
    """従来の選べるセット表（内訳JANコード列なし）を返す。"""
    return choice_detail.loc[:, ["商品名", "発注数量", "備考"]].reset_index(drop=True)


def _build_choice_products_detail(
    orders: pd.DataFrame,
    masters: MasterTables,
    warnings: list[str],
) -> pd.DataFrame:
    """選べるセット集計の内部版。合算表（依頼2）で使う内訳JANコード列を保持して返す。"""
    empty = pd.DataFrame(columns=["内訳JANコード", "商品名", "発注数量", "備考"])
    work = _not_ordered_rows(orders)[
        ["商品ｺｰﾄﾞ", "商品ｵﾌﾟｼｮﾝ", "受注数", "引当数", "作業者欄"]
    ].copy()

    choice_keys = masters.choice_master[["NEコード"]].drop_duplicates(subset=["NEコード"])
    work = work.merge(choice_keys, left_on="商品ｺｰﾄﾞ", right_on="NEコード", how="inner")
    if work.empty:
        return empty

    work = work.drop(columns=["NEコード", "作業者欄"])
    expanded = _expand_options(work, warnings)
    if expanded.empty:
        return empty

    expanded["商品ｺｰﾄﾞ"] = expanded["商品ｺｰﾄﾞ"].replace(
        {"bireleysaraberuset": "a009-2215-c01"}
    )
    expanded["商品ｵﾌﾟｼｮﾝ.2"] = expanded["商品ｵﾌﾟｼｮﾝ.2"].replace({"ＮＯ.７": "NO.7"})

    choice_lookup = masters.choice_master[
        ["NEコード", "項目選択肢項目名", "項目選択肢", "JANコード", "数量"]
    ].copy()
    expanded = expanded.merge(
        choice_lookup,
        left_on=["商品ｺｰﾄﾞ", "商品ｵﾌﾟｼｮﾝ.1", "商品ｵﾌﾟｼｮﾝ.2"],
        right_on=["NEコード", "項目選択肢項目名", "項目選択肢"],
        how="left",
    )
    expanded = expanded.rename(columns={"JANコード": "内訳JANコード", "数量": "内訳数量"})
    expanded["内訳数量"] = pd.to_numeric(expanded["内訳数量"], errors="coerce").fillna(0)

    missing_choice = expanded["内訳JANコード"].fillna("").eq("").sum()
    if missing_choice:
        warnings.append(f"選べるセットの内訳マスタ未一致が {missing_choice} 行あります。")

    product_lookup = masters.product_master[["JANコード", "商品名"]].drop_duplicates(
        subset=["JANコード"]
    )
    expanded = expanded.merge(
        product_lookup,
        left_on="内訳JANコード",
        right_on="JANコード",
        how="left",
        suffixes=("", "_商品マスタ"),
    )
    expanded = expanded.rename(columns={"商品名": "内訳商品名"})
    expanded["内訳商品名"] = expanded["内訳商品名"].fillna("")
    expanded["受注数×内訳数量"] = expanded["受注数"] * expanded["内訳数量"]

    grouped = (
        expanded.groupby(["内訳JANコード", "内訳商品名"], dropna=False, as_index=False)[
            "受注数×内訳数量"
        ]
        .sum()
        .rename(columns={"内訳商品名": "商品名", "受注数×内訳数量": "発注数量"})
    )
    grouped["内訳JANコード"] = grouped["内訳JANコード"].fillna("")
    grouped["発注数量"] = grouped["発注数量"].astype(int)
    grouped["備考"] = "選べるセット"
    return (
        grouped.loc[:, ["内訳JANコード", "商品名", "発注数量", "備考"]]
        .sort_values("商品名")
        .reset_index(drop=True)
    )


def build_combined_products(
    normal: pd.DataFrame,
    choice_detail: pd.DataFrame,
    masters: MasterTables,
) -> pd.DataFrame:
    """通常商品とセット内訳をJANコードで対応付けた数量合算表（依頼2）。

    確定仕様（2026-07-20 ユーザー回答）:
    - 列は 商品コード / 商品名 / 必要数 / 備考 の4列。
    - 必要数 = 通常商品の受注数 + 選べるセットの発注数量（受注数×内訳数量）。
    - 引当数はセット側に対応する値が無いため合算表には載せない。
    - 備考は由来を表示: 「通常のみ」「セット含む」「セットのみ」。
    - 対応付けはJANコード（通常商品のNEコードは商品マスタでJANに解決する）。
      JANに解決できない行は合算されず、そのまま独立行として残す。
    """
    normal_work = normal.copy() if not normal.empty else pd.DataFrame(
        columns=["商品ｺｰﾄﾞ", "商品名", "受注数", "引当数"]
    )
    choice_work = choice_detail.copy() if not choice_detail.empty else pd.DataFrame(
        columns=["内訳JANコード", "商品名", "発注数量", "備考"]
    )

    ne_to_jan = (
        masters.product_master[["NEコード", "JANコード"]]
        .drop_duplicates(subset=["NEコード"])
        .copy()
        if {"NEコード", "JANコード"}.issubset(masters.product_master.columns)
        else pd.DataFrame(columns=["NEコード", "JANコード"])
    )

    normal_work = normal_work.merge(
        ne_to_jan, left_on="商品ｺｰﾄﾞ", right_on="NEコード", how="left"
    )
    normal_work["JANコード"] = normal_work["JANコード"].fillna("").astype(str)
    # 結合キー: JANがあればJAN。無ければ衝突しない接頭辞付きコードで独立行にする
    normal_jan = normal_work["JANコード"]
    normal_work["_key"] = normal_jan.where(
        normal_jan != "", "NE::" + normal_work["商品ｺｰﾄﾞ"].astype(str)
    )
    # 同一JANに複数のNEコードが解決される場合の二重計上を防ぐため、キー単位で先に合算する
    # 注: キーワード引数名はNFKC正規化で半角カナが化けるため dict 形式で列名を指定する
    normal_grouped = normal_work.groupby("_key", as_index=False).agg(
        {
            "商品ｺｰﾄﾞ": lambda s: "／".join(dict.fromkeys(s.astype(str))),
            "商品名": "first",
            "受注数": "sum",
        }
    )

    choice_work["内訳JANコード"] = choice_work["内訳JANコード"].fillna("").astype(str)
    choice_jan = choice_work["内訳JANコード"]
    choice_work["_key"] = choice_jan.where(
        choice_jan != "", "CH::" + choice_work["商品名"].astype(str)
    )

    merged = normal_grouped.loc[:, ["_key", "商品ｺｰﾄﾞ", "商品名", "受注数"]].merge(
        choice_work.loc[:, ["_key", "内訳JANコード", "商品名", "発注数量"]],
        on="_key",
        how="outer",
        suffixes=("_通常", "_セット"),
    )

    merged["受注数"] = pd.to_numeric(merged["受注数"], errors="coerce").fillna(0).astype(int)
    merged["発注数量"] = (
        pd.to_numeric(merged["発注数量"], errors="coerce").fillna(0).astype(int)
    )
    has_normal = merged["商品ｺｰﾄﾞ"].notna()
    has_choice = merged["商品名_セット"].notna()

    merged["必要数"] = merged["受注数"] + merged["発注数量"]
    merged["商品コード"] = (
        merged["商品ｺｰﾄﾞ"].fillna("").astype(str).where(has_normal, merged["内訳JANコード"].fillna(""))
    )
    normal_names = merged["商品名_通常"].fillna("").astype(str)
    choice_names = merged["商品名_セット"].fillna("").astype(str)
    merged["商品名"] = normal_names.where(normal_names != "", choice_names)
    merged["備考"] = "通常のみ"
    merged.loc[has_normal & has_choice, "備考"] = "セット含む"
    merged.loc[~has_normal & has_choice, "備考"] = "セットのみ"

    return (
        merged.loc[:, ["商品コード", "商品名", "必要数", "備考"]]
        .sort_values(["商品コード", "商品名"])
        .reset_index(drop=True)
    )


def result_to_csv(result: InventoryResult, kind: Literal["normal", "choice"]) -> str:
    rows = result.normal_rows if kind == "normal" else result.choice_rows
    frame = pd.DataFrame(rows)
    buffer = StringIO()
    frame.to_csv(buffer, index=False, encoding="utf-8-sig")
    return buffer.getvalue()


def _clean_columns(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    return frame


def _not_ordered_rows(orders: pd.DataFrame) -> pd.DataFrame:
    return orders[~orders["作業者欄"].astype(str).str.contains("発注", na=False)].copy()


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


def _expand_options(work: pd.DataFrame, warnings: list[str]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for row in work.to_dict(orient="records"):
        raw_options = str(row.get("商品ｵﾌﾟｼｮﾝ", "")).replace("　", "|").replace(" ", "|")
        for token in [part for part in raw_options.split("|") if part]:
            if ":" in token:
                name, value = token.split(":", 1)
            elif "：" in token:
                name, value = token.split("：", 1)
            else:
                warnings.append(f"商品オプションを分割できませんでした: {token}")
                continue

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


def _records(frame: pd.DataFrame) -> list[dict[str, object]]:
    if frame.empty:
        return []
    clean = frame.where(pd.notna(frame), "")
    return clean.to_dict(orient="records")
