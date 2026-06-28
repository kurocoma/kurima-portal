from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Literal
import warnings

import pandas as pd

from portal_app.services.paths import PortalPaths, find_portal_paths, latest_order_csv

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

    @property
    def normal_count(self) -> int:
        return len(self.normal_rows)

    @property
    def choice_count(self) -> int:
        return len(self.choice_rows)


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
    choice = build_choice_products(orders, masters, warnings)

    return InventoryResult(
        generated_at=datetime.now(),
        paths=paths,
        source_csv=source_csv,
        source_rows=len(orders),
        normal_rows=_records(normal),
        choice_rows=_records(choice),
        warnings=warnings,
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
    work = _not_ordered_rows(orders)[
        ["商品ｺｰﾄﾞ", "商品ｵﾌﾟｼｮﾝ", "受注数", "引当数", "作業者欄"]
    ].copy()

    choice_keys = masters.choice_master[["NEコード"]].drop_duplicates(subset=["NEコード"])
    work = work.merge(choice_keys, left_on="商品ｺｰﾄﾞ", right_on="NEコード", how="inner")
    if work.empty:
        return pd.DataFrame(columns=["商品名", "発注数量", "備考"])

    work = work.drop(columns=["NEコード", "作業者欄"])
    expanded = _expand_options(work, warnings)
    if expanded.empty:
        return pd.DataFrame(columns=["商品名", "発注数量", "備考"])

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
    grouped = grouped.drop(columns=["内訳JANコード"])
    grouped["発注数量"] = grouped["発注数量"].astype(int)
    grouped["備考"] = "選べるセット"
    return grouped.loc[:, ["商品名", "発注数量", "備考"]].sort_values("商品名").reset_index(
        drop=True
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
