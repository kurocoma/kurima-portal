"""ヤマト系フロー（ヤマト伝票 / ネコポス）の検索条件・保存先プロファイル。

2026-07-20 依頼5「ネコポスの新規カード作成」:
ネコポスは「基本的にはヤマト伝票CSVと同じ」だが、以下だけが異なる。

- 対象受注: 受注一覧の「ネコポス」ボタン。実機調査（2026-07-21）で、これは保存検索
  ではなく**オリジナルステータス検索**（originalStatus.search(118)）であることを確認した
  （依頼メモのURL search_condi=16 はステータスバーの「印刷日待」リンクで別物。
  検証用受注 69589/69586/69585 は originalStatus.search(118) でちょうど3件表示された）。
  着地後の検索ダイアログ再検索は行わない（shipping_options 空 = 発送方法再選択スキップ。
  再検索するとオリジナルステータス条件が外れるため）。
- 保存先: 既存ヤマトと同じフォルダ・prefixにすると「最新ファイル自動選択」が
  混線するため、配送情報CSV・B2取込CSVともに別prefixへ分離する。
- 送り状種別: B2のネコポスコード(7)へ変換時に上書きし、ネコポスで指定できない
  列（温度区分・配達指定日・時間指定・コレクト額・営業所止置き）を空欄化する。
  NEカスタムパターンが実際に何を出力するかは検証用受注で確認し、値が既に7なら
  上書きは無変更（警告も出ない）。

既存のヤマト伝票フローは YAMATO_PROFILE（従来値そのまま）で動き、挙動は変わらない。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class YamatoFlowProfile:
    key: str
    label: str
    # NE受注一覧: 着地URL（保存検索 search_condi 付き、またはプレーンな一覧）
    order_list_url: str
    # 検索ダイアログで再選択する発送方法（空タプル=着地時の条件を正として再検索しない）
    shipping_options: tuple[str, ...]
    # 着地後に適用するオリジナルステータス検索のID（None=適用しない）。
    # 受注一覧の「ネコポス」等のバッジボタンは originalStatus.search(ID) を呼ぶ実装。
    original_status_id: int | None
    # 配送情報CSV（NEカスタムデータDL）で使うカスタムパターン名。
    # 注: 既存パターン「新【共通】ヤマトB2V6（店舗名出力）」は発送方法28(ヤマト(ネコポス))を
    # 対象外にしており（2026-07-21実機確認: ダウンロードできるデータはありません）、
    # ネコポス運用にはNE側でパターンの対象に28を追加するか、ネコポス用パターンの新設が必要。
    custom_delivery_pattern: str
    # 配送情報CSV（NEカスタムデータDL）の保存フォルダ名とファイル名prefix
    source_dir_name: str
    source_prefix: str
    # 購入者データ・商品情報データのファイル名prefix
    data_prefix: str
    # B2取込CSV（完成データ）のファイル名prefix
    output_prefix: str
    # 変換時に上書きする送り状種別（None=入力CSVの値をそのまま使う）
    invoice_type_override: str | None
    # ネコポスで指定できない列（温度区分・配達指定日等）を変換時に空欄化するか
    clear_unsupported_columns: bool


YAMATO_PROFILE = YamatoFlowProfile(
    key="yamato",
    label="ヤマト伝票",
    order_list_url="https://main.next-engine.com/Userjyuchu/index?search_condi=17",
    shipping_options=(
        "20 : ヤマト(発払い)B2v6",
        "21 : ヤマト(コレクト)B2v6",
    ),
    original_status_id=None,
    custom_delivery_pattern="新【共通】ヤマトB2V6（店舗名出力）",
    source_dir_name="ne-yamatocsv",
    source_prefix="ne-yamato",
    data_prefix="dataヤマト",
    output_prefix="ne-to-yamato",
    invoice_type_override=None,
    clear_unsupported_columns=False,
)

# B2クラウドの送り状種別コード: ネコポス=**A**（2026-07-21 B2実機のJS定数
# SERVICE_TYPE_NEKOPOS_CD='A' から取得）。「7」は現行B2では**クロネコゆうパケット**
# （SERVICE_TYPE_YUPACKET_CD='7'）に割り当てられており、NEカスタムパターンが出力する
# 「7」をそのまま通すとゆうパケット扱いで取り込まれる（実障害で確認）。
# 参考: 0=発払い 1=EAZY 2=コレクト 3=クロネコゆうメール 4=タイム 6=発払い(複数口)
#       7=クロネコゆうパケット 8=宅急便コンパクト 9=コンパクトコレクト A=ネコポス
NEKOPOS_INVOICE_TYPE = "A"
# NEのオリジナルステータス「ネコポス」のID（受注一覧のバッジ #original_status_118 から確認）
NEKOPOS_ORIGINAL_STATUS_ID = 118

NEKOPOS_PROFILE = YamatoFlowProfile(
    key="nekopos",
    label="ネコポス",
    order_list_url="https://main.next-engine.com/Userjyuchu/index",
    shipping_options=tuple(),
    original_status_id=NEKOPOS_ORIGINAL_STATUS_ID,
    # 暫定: ヤマトと同じパターンを使う（NE側で対象に発送方法28を追加すればそのまま動く。
    # ネコポス専用パターンを新設した場合はここを変更する）
    custom_delivery_pattern="新【共通】ヤマトB2V6（店舗名出力）",
    source_dir_name="ne-nekoposcsv",
    source_prefix="ne-nekopos",
    data_prefix="dataネコポス",
    output_prefix="ne-to-nekopos",
    invoice_type_override=NEKOPOS_INVOICE_TYPE,
    clear_unsupported_columns=True,
)

_PROFILES = {profile.key: profile for profile in (YAMATO_PROFILE, NEKOPOS_PROFILE)}


def profile_for_mode(mode: str | None) -> YamatoFlowProfile:
    """フォーム/クエリの mode 値からプロファイルを解決する。不明値はヤマト（従来挙動）。"""
    return _PROFILES.get((mode or "").strip().lower(), YAMATO_PROFILE)
