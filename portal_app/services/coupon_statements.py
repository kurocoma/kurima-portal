"""クーポン明細取得（楽天RMS 受注データ「クーポン」テンプレート）。

前月分（注文日基準・月初 00:00:00 〜 月末 23:59:59）のクーポン明細CSVを
RMSの受注データダウンロード画面から取得し、

    入金明細/クーポン/csv/{YYMM}_coupon.csv

へ保存したうえで、テンプレート xlsm を「{N}月クーポン利用 .xlsm」として複製する。

一次情報: Obsidian「楽天 日別売上CSVダウンロード（Playwright フロー）」
（20-claude-docs/rakuten-playwright-daily-sales-download.md）と、その原本実装
`開発案件/日別売上集計データダウンロード/src/downloader/rakuten.py`。

ログイン（最大5画面: R-Login → 楽天会員ID → PW → 条件付き3画面。最後の
「RMSメインメニューへ進む」を踏まないとセッションが確立しない）は、既に稼働実績のある
`access_analytics_rakuten._attempt_rakuten_login` をそのまま再利用する。
DOM操作も手順書の契約（fromYmd / toYmd を ISO value で select、#dataCreateBtn、
0件は「データ件数は0件です」で正常スキップ、input#user + downloadPassword の第3認証、
expect_download 内で #downloadBtn）をそのまま踏襲する。

日別売上フローとの差分（クーポン明細固有）:
- 日付種別は発送日（input#r06）ではなく「注文日」
- 出力テンプレートは全カラム（value="-1"）ではなく「クーポン」（value="3"）
- 期間は対象日1日ではなく前月の月初〜月末（fromYmd ≠ toYmd）
- データ作成待ちは 120 秒ではなく既定 1800 秒（10分以上かかる月があるため）

複数PC対応: 保存先は KURIMA_COUPON_DIR / KURIMA_PORTAL_ROOT から解決する
（paths.find_coupon_paths）。複製した xlsm には、その PC で解決した実パスと
対象CSVファイル名を Power Query の参照セル（setup[filePath] / setup_fileName[fileName]）
へ**数式を落とした静的な文字列として**書き込むため、テンプレートに焼き付いた
他ユーザーの絶対パス（C:\\Users\\kamiz\\...）にも、開いた月の再計算にも引きずられない。

安全側の作り（実運用で効くもの）:
- 保存先がロック（Excelで開いたまま）なら state="FILE_LOCKED" で止めて開き直しを促す
- ダウンロード物が cp932 CSV に見えないときは保存せず隔離（既存CSVを壊さない）
- データ作成待ちが上限に達したら「ダウンロード利用履歴」からの回収を試みる
- 長い待機ループから cancel_check を呼び、「実行を中止」で確実に止まる
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from portal_app.env import env_int
from portal_app.services.access_analytics_rakuten import (
    RAKUTEN_LOGIN_ENTRY_URL,
    AccessAnalyticsError,
    _attempt_rakuten_login,
)
from portal_app.services.execution_logger import APP_ROOT
from portal_app.services.paths import CouponPaths, find_coupon_paths
from portal_app.services.progress_jobs import JobCancelled
from portal_app.settings import download_timeout_ms, nav_timeout_ms


# 受注データダウンロード画面（手順書と同じ直URL。#result まで含めるのが正）。
CSV_DOWNLOAD_URL = (
    "https://csvdl-rp.rms.rakuten.co.jp/rms/mall/csvdl/CD02_01_001?dataType=opp_order#result"
)
CSV_DOWNLOAD_HOST = "csvdl-rp.rms.rakuten.co.jp"
# 出力テンプレート「クーポン」= option value="3"（ユーザー観測）。
# 日別売上フローの value="-1"（全カラム）とは別物。番号は将来変わり得るため、
# 実行時にラベル（「クーポン」）で必ず裏取りしてから選択する。
COUPON_TEMPLATE_ID = "3"
COUPON_TEMPLATE_LABEL = "クーポン"
# 日付種別。手順書の r06 は「発送日」なので絶対に選ばない（クーポンは注文日時基準）。
ORDER_DATE_LABEL_KEYWORD = "注文日"
SHIPPING_DATE_RADIO_ID = "r06"

_PROFILE_ENV = "KURIMA_COUPON_CHROME_PROFILE"
# 認証情報は3種（手順書 src/credential.py の _SITE_KEY_MAP と同じ区分）。
#   楽天(管理)         → R-Login          … KURIMA_RAKUTEN_KANRI_LOGIN_ID / _PASSWORD
#   楽天(ユーザー認証) → 楽天会員ログイン … KURIMA_RAKUTEN_LOGIN_ID / _PASSWORD
#   楽天(ダウンロード用)→ CSV DL 直前の第3認証 … 下記 env（未設定なら ID・PW.xlsx）
_CSV_USER_ENV = "KURIMA_RAKUTEN_CSV_DL_USER"
_CSV_PASSWORD_ENV = "KURIMA_RAKUTEN_CSV_DL_PASSWORD"
_RAKUTEN_ENV_FALLBACK: tuple[tuple[str, str, str], ...] = (
    ("楽天(管理)", "KURIMA_RAKUTEN_KANRI_LOGIN_ID", "KURIMA_RAKUTEN_KANRI_PASSWORD"),
    ("楽天(ユーザー認証)", "KURIMA_RAKUTEN_LOGIN_ID", "KURIMA_RAKUTEN_LOGIN_PASSWORD"),
)
# 「データを作成する」→ 作成完了までの待ち時間上限（秒）。大量データは10分以上かかる。
_DATA_WAIT_SECONDS_ENV = "KURIMA_COUPON_DATA_WAIT_SECONDS"
_DEFAULT_DATA_WAIT_SECONDS = 1_800

_NO_DATA_MARKERS = (
    "この条件でのデータ件数は0件です",
    "データ件数は0件",
    "該当するデータがありません",
)
_AUTH_REQUIRED_MARKER = "認証が必要です"
# データ作成が待ち上限を超えたときの救済経路（task 明記）。
# RMSは作成済みデータを一定期間「ダウンロード利用履歴」から再取得できる。
_DOWNLOAD_HISTORY_LABEL = "ダウンロード利用履歴"
_HISTORY_URL_ENV = "KURIMA_COUPON_HISTORY_URL"
_HISTORY_DOWNLOAD_LABEL = "ダウンロード"
# ダウンロード物の妥当性検証（認証エラーHTML等で正常CSVを壊さないための門番）。
# 楽天RMSの受注データCSVは cp932・カンマ区切り。手順書の列名（注文番号/ステータス 等）を
# ヘッダー行に含むはずなので、1つも無ければクーポン明細CSVとみなさない。
_CSV_VALIDATION_BYTES = 64 * 1024
_CSV_HEADER_KEYWORDS = (
    "注文番号",
    "受注番号",
    "ステータス",
    "クーポン",
    "注文日",
)
_HTML_MARKERS = ("<html", "<!doctype html", "<head", "<script", "<meta")
# 検証に落ちたダウンロード物の隔離先（上書きせずに残して調査できるようにする）。
_QUARANTINE_DIRNAME = "quarantine"
# 複製先の xlsm 名は運用実績どおり「{N}月クーポン利用 .xlsm」（拡張子前に半角スペース）。
# 既存ファイルは表記ゆれ（スペース無し・全角数字）があるため、重複判定は広めに取る。
_WORKBOOK_SUFFIX = "月クーポン利用"
_FULLWIDTH_DIGITS = "０１２３４５６７８９"

# Power Query が参照するセル（テンプレートの「環境設定」シート = xl/worksheets/sheet1.xml）。
#   A2 = setup[filePath]      … クーポンフォルダの絶対パス（末尾 \ 必須）
#   A9 = setup_fileName[fileName] … {YYMM}_coupon.csv
# M式は filePath & "csv\" & fileName で読むため、A2 は必ず区切り文字で終える。
_FILE_PATH_CELL = "A2"
_FILE_NAME_CELL = "A9"
_SHEET_XML_NAME = "xl/worksheets/sheet1.xml"
# 複製ブックでは A2/A9 を「数式なしの文字列セル」に落とす（下の stamp_workbook_source 参照）。
# 数式を消すと calcChain.xml の参照が実体と食い違い、Excel が修復ダイアログを出すことがある。
# calcChain は Excel が再生成する派生パートなので、消すのが定石。
_CALC_CHAIN_NAME = "xl/calcChain.xml"
_CONTENT_TYPES_NAME = "[Content_Types].xml"
_WORKBOOK_RELS_NAME = "xl/_rels/workbook.xml.rels"


class CouponStatementsError(RuntimeError):
    """利用者向け state を保持するクーポン明細取得エラー。"""

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


@dataclass(frozen=True)
class CouponPeriod:
    """取得対象（前月）の期間とファイル命名。"""

    year: int
    month: int
    start_date: date
    end_date: date

    @property
    def yymm(self) -> str:
        """例: 2026年7月 → "2607"。"""
        return f"{self.year % 100:02d}{self.month:02d}"

    @property
    def csv_name(self) -> str:
        return f"{self.yymm}_coupon.csv"

    @property
    def workbook_name(self) -> str:
        return f"{self.month}{_WORKBOOK_SUFFIX} .xlsm"

    @property
    def label(self) -> str:
        return f"{self.year}年{self.month}月"

    @property
    def start_text(self) -> str:
        return self.start_date.isoformat()

    @property
    def end_text(self) -> str:
        return self.end_date.isoformat()


@dataclass(frozen=True)
class CouponStatementsResult:
    executed: bool
    period: CouponPeriod
    csv_path: Path | None
    csv_bytes: int | None
    csv_rows: int | None
    workbook_path: Path | None
    workbook_created: bool
    skipped_reason: str | None
    warnings: tuple[str, ...]


# --------------------------------------------------------------------------
# 純ロジック（期間計算・命名・パス解決・xlsm 書き換え）
# --------------------------------------------------------------------------
def previous_month(today: date | None = None) -> tuple[int, int]:
    """実行日の前月を (year, month) で返す。1月実行なら前年12月。"""

    base = today or date.today()
    if base.month == 1:
        return base.year - 1, 12
    return base.year, base.month - 1


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - date(year, month, 1)).days


def resolve_period(today: date | None = None) -> CouponPeriod:
    """前月の月初〜月末（注文日基準）とファイル命名をまとめて返す純関数。"""

    year, month = previous_month(today)
    return CouponPeriod(
        year=year,
        month=month,
        start_date=date(year, month, 1),
        end_date=date(year, month, _last_day_of_month(year, month)),
    )


def workbook_name_variants(month: int) -> tuple[str, ...]:
    """既存の複製先とみなす表記ゆれ一覧（重複作成を防ぐための安全側判定）。"""

    fullwidth = "".join(_FULLWIDTH_DIGITS[int(digit)] for digit in str(month))
    names = []
    for number in (str(month), fullwidth):
        names.append(f"{number}{_WORKBOOK_SUFFIX} .xlsm")
        names.append(f"{number}{_WORKBOOK_SUFFIX}.xlsm")
    return tuple(dict.fromkeys(names))


def find_existing_workbook(root: Path, month: int) -> Path | None:
    """その月のクーポン利用ブックが既にあれば返す（表記ゆれを含めて探す）。"""

    for name in workbook_name_variants(month):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def replace_cached_cell_value(sheet_xml: str, cell_ref: str, value: str) -> str:
    """シートXMLの指定セルのキャッシュ値 `<v>` だけを差し替える。

    数式（`<f>`）は残したままキャッシュ値を正しい値にする。マクロが無効で
    UDF（OneDriveUrlToLocalPath）が評価できない環境でも、Power Query は
    このキャッシュ値を読むため、他ユーザーの絶対パスを引かなくなる。
    """

    pattern = re.compile(rf'(<c r="{re.escape(cell_ref)}"(?:\s[^>]*?)?>)(.*?)(</c>)', re.S)
    match = pattern.search(sheet_xml)
    if match is None:
        raise CouponStatementsError(
            f"テンプレートのセル {cell_ref} を特定できませんでした。",
            state="TEMPLATE_CONTRACT_CHANGED",
        )
    body = match.group(2)
    replacement = f"<v>{xml_escape(value)}</v>"
    if "<v>" in body:
        new_body = re.sub(r"<v>.*?</v>", lambda _: replacement, body, count=1, flags=re.S)
    else:
        new_body = body + replacement
    head = sheet_xml[: match.start()]
    tail = sheet_xml[match.end() :]
    return head + match.group(1) + new_body + match.group(3) + tail


def set_static_cell_value(sheet_xml: str, cell_ref: str, value: str) -> str:
    """指定セルを「数式なしのインライン文字列セル」へ置き換える。

    テンプレートの A2/A9 は volatile 数式（`ca="1"`。OneDriveUrlToLocalPath・TODAY）
    なので、複製ブックを Excel で開いた瞬間に再計算され、書き込んだキャッシュ値が
    上書きされる（マクロ無効PCでは #NAME? になる）。複製側は数式ごと落として
    静的な文字列にしておく＝どのPC・どの月に開いても値が動かない。
    書式（`s=` 属性）は保持し、`t=` だけ inlineStr に付け替える。
    """

    pattern = re.compile(
        rf'<c r="{re.escape(cell_ref)}"((?:\s[^>]*?)?)(?:/>|>(?:.*?)</c>)', re.S
    )
    match = pattern.search(sheet_xml)
    if match is None:
        raise CouponStatementsError(
            f"テンプレートのセル {cell_ref} を特定できませんでした。",
            state="TEMPLATE_CONTRACT_CHANGED",
        )
    attributes = re.sub(r'\s+t="[^"]*"', "", match.group(1) or "").rstrip("/")
    cell = (
        f'<c r="{cell_ref}"{attributes} t="inlineStr">'
        f'<is><t xml:space="preserve">{xml_escape(value)}</t></is></c>'
    )
    return sheet_xml[: match.start()] + cell + sheet_xml[match.end() :]


def remove_calc_chain_override(content_types_xml: str) -> str:
    """[Content_Types].xml から calcChain の Override を削除する。"""

    return re.sub(
        r'<Override[^>]*PartName="/xl/calcChain\.xml"[^>]*/>', "", content_types_xml
    )


def remove_calc_chain_relationship(rels_xml: str) -> str:
    """xl/_rels/workbook.xml.rels から calcChain の Relationship を削除する。"""

    return re.sub(r'<Relationship[^>]*Target="calcChain\.xml"[^>]*/>', "", rels_xml)


def stamp_workbook_source(
    workbook_path: Path,
    *,
    file_path_value: str,
    file_name_value: str,
    static: bool = True,
) -> None:
    """複製した xlsm の Power Query 参照セルへ、このPCの実パスと対象CSV名を書き込む。

    xlsm は zip なので、`xl/worksheets/sheet1.xml` だけ差し替えて再梱包する
    （vbaProject.bin・customXml の DataMashup など他パートはバイト列のまま複写）。

    static=True（既定・複製ブック向け）は A2/A9 を数式ごと静的な文字列セルにして、
    再計算・マクロ無効環境の影響を受けないようにする。あわせて派生パートの
    calcChain.xml を落とす（数式を消した状態と食い違うと Excel が修復を促すため。
    Excel が開いたときに再生成する）。
    static=False は数式を残してキャッシュ値だけ差し替える（テンプレート自体の
    再計算経路を壊したくない場合向け）。
    """

    if not file_path_value.endswith(("\\", "/")):
        file_path_value = file_path_value + "\\"

    with zipfile.ZipFile(workbook_path) as source:
        names = source.namelist()
        if _SHEET_XML_NAME not in names:
            raise CouponStatementsError(
                "テンプレートに環境設定シート（sheet1.xml）がありません。",
                state="TEMPLATE_CONTRACT_CHANGED",
            )
        entries = [(info, source.read(info.filename)) for info in source.infolist()]

    write_cell = set_static_cell_value if static else replace_cached_cell_value
    drop_calc_chain = static and _CALC_CHAIN_NAME in names

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for info, data in entries:
            if drop_calc_chain and info.filename == _CALC_CHAIN_NAME:
                continue
            if info.filename == _SHEET_XML_NAME:
                sheet_xml = data.decode("utf-8")
                sheet_xml = write_cell(sheet_xml, _FILE_PATH_CELL, file_path_value)
                sheet_xml = write_cell(sheet_xml, _FILE_NAME_CELL, file_name_value)
                data = sheet_xml.encode("utf-8")
            elif drop_calc_chain and info.filename == _CONTENT_TYPES_NAME:
                data = remove_calc_chain_override(data.decode("utf-8")).encode("utf-8")
            elif drop_calc_chain and info.filename == _WORKBOOK_RELS_NAME:
                data = remove_calc_chain_relationship(data.decode("utf-8")).encode("utf-8")
            new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new_info.compress_type = info.compress_type
            new_info.external_attr = info.external_attr
            new_info.internal_attr = info.internal_attr
            new_info.create_system = info.create_system
            target.writestr(new_info, data)
    workbook_path.write_bytes(buffer.getvalue())


def replace_file(temporary: Path, destination: Path, *, label: str) -> None:
    """一時ファイルを保存先へ確定する（ロック・別ドライブを型付きエラー／代替経路にする）。

    OneDrive/SharePoint 同期フォルダが保存先なので、
    「保存先を Excel で開いたまま実行」は最も現実的な失敗経路。生の PermissionError
    ではなく state="FILE_LOCKED" と日本語ガイドに変換して、UI で何をすればよいか出す。
    別ドライブ間（EXDEV）で `Path.replace` が失敗する構成では shutil.move へ退避する。
    """

    try:
        temporary.replace(destination)
        return
    except PermissionError as exc:
        raise CouponStatementsError(
            f"{destination.name} を保存できませんでした（ファイルが使用中です）。"
            f"{label}を Excel などで開いていたら閉じてから、もう一度実行してください。",
            state="FILE_LOCKED",
        ) from exc
    except OSError as exc:
        # 別ドライブ・別ボリューム間の rename（EXDEV）などは move で救える。
        try:
            shutil.move(str(temporary), str(destination))
            return
        except PermissionError as move_exc:
            raise CouponStatementsError(
                f"{destination.name} を保存できませんでした（ファイルが使用中です）。"
                f"{label}を Excel などで開いていたら閉じてから、もう一度実行してください。",
                state="FILE_LOCKED",
            ) from move_exc
        except OSError as move_exc:
            raise CouponStatementsError(
                f"{destination.name} を保存できませんでした（{move_exc}）。"
                "保存先フォルダの空き容量とアクセス権を確認してください。",
                state="SAVE_FAILED",
            ) from exc


def copy_coupon_workbook(
    paths: CouponPaths,
    period: CouponPeriod,
    *,
    stamp: bool = True,
) -> tuple[Path | None, bool, list[str]]:
    """テンプレートを「{N}月クーポン利用 .xlsm」として複製する。

    既存ファイルがあれば**上書きせずスキップ**する（安全側）。
    戻り値は (複製先パス, 新規作成したか, 警告リスト)。
    """

    warnings: list[str] = []
    existing = find_existing_workbook(paths.root, period.month)
    if existing is not None:
        warnings.append(
            f"{existing.name} が既にあるため、ブックの複製はスキップしました（上書きしません）。"
        )
        return existing, False, warnings

    if not paths.template_path.is_file():
        warnings.append(
            f"テンプレート（{paths.template_path.name}）が見つからないため複製をスキップしました。"
        )
        return None, False, warnings

    destination = paths.root / period.workbook_name
    temporary = paths.root / f".{uuid4().hex}_{period.workbook_name}"
    try:
        shutil.copy2(paths.template_path, temporary)
        if stamp:
            try:
                stamp_workbook_source(
                    temporary,
                    file_path_value=str(paths.root),
                    file_name_value=period.csv_name,
                )
            except Exception as exc:  # 書き換え失敗時も素の複製は残す（フェイルセーフ）
                warnings.append(
                    "複製したブックのデータ参照先（filePath/fileName）を書き換えられませんでした。"
                    f"Excelで手動確認してください: {exc}"
                )
        # 直前に他PC・他ジョブが作った場合に備え、置換の瞬間まで存在を確認する。
        if destination.exists():
            warnings.append(
                f"{destination.name} が既にあるため、ブックの複製はスキップしました"
                "（上書きしません）。"
            )
            temporary.unlink(missing_ok=True)
            return destination, False, warnings
        replace_file(temporary, destination, label="複製先のブック")
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination, True, warnings


def read_cell_text(sheet_xml: str, cell_ref: str) -> str:
    """セルの文字列値を読む（インライン文字列 `<is><t>` と キャッシュ値 `<v>` の両対応）。"""

    match = re.search(
        rf'<c r="{re.escape(cell_ref)}"(?:\s[^>]*?)?(?:/>|>(.*?)</c>)', sheet_xml, re.S
    )
    if match is None:
        return ""
    body = match.group(1) or ""
    inline = re.search(r"<is>.*?<t[^>]*>(.*?)</t>.*?</is>", body, re.S)
    if inline is not None:
        return xml_unescape(inline.group(1))
    cached = re.search(r"<v>(.*?)</v>", body, re.S)
    return xml_unescape(cached.group(1)) if cached is not None else ""


def read_workbook_source_cells(workbook_path: Path) -> dict[str, str]:
    """xlsm の filePath / fileName の値を読む（テンプレ修正の検証・プレビュー用）。"""

    with zipfile.ZipFile(workbook_path) as book:
        sheet_xml = book.read(_SHEET_XML_NAME).decode("utf-8")
    return {
        "filePath": read_cell_text(sheet_xml, _FILE_PATH_CELL),
        "fileName": read_cell_text(sheet_xml, _FILE_NAME_CELL),
    }


# --------------------------------------------------------------------------
# 実行時のヘルパー
# --------------------------------------------------------------------------
def _profile_dir() -> Path:
    override = os.environ.get(_PROFILE_ENV, "").strip()
    return Path(override) if override else APP_ROOT / "data" / "coupon_chrome_profile"


def _headless_value(value: bool | None) -> bool:
    if value is not None:
        return value
    return os.environ.get("KURIMA_BROWSER_HEADLESS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _data_wait_seconds() -> int:
    return env_int(_DATA_WAIT_SECONDS_ENV, _DEFAULT_DATA_WAIT_SECONDS, minimum=30)


# 「実行を中止」が押されたかを問い合わせるコールバック（progress_jobs.is_cancel_requested）。
CancelCheck = Callable[[], bool]


def raise_if_cancelled(cancel_check: CancelCheck | None) -> None:
    """中止要求があれば JobCancelled を送出する。

    このカードは5〜20分（データ量次第でそれ以上）走るため、待機ループの中から
    定期的に呼ぶ。progress_jobs 側は worker の例外を見て「中止」として確定する。
    """

    if cancel_check is not None and cancel_check():
        raise JobCancelled()


def load_csv_download_credential() -> tuple[str, str]:
    """CSVダウンロード用のID/PWを解決する。

    環境変数（KURIMA_RAKUTEN_CSV_DL_USER / _PASSWORD）が最優先。未設定なら
    既存資産の認証情報Excel（ID・PW.xlsx）の「楽天(ダウンロード用)」行から読む。
    値はローカル変数としてのみ扱い、ログ・戻り値・例外メッセージへ載せない。
    """

    user = os.environ.get(_CSV_USER_ENV, "").strip()
    password = os.environ.get(_CSV_PASSWORD_ENV, "").strip()
    if user and password:
        return user, password

    try:
        from portal_app.services.credentials import load_site_credential

        credential = load_site_credential("楽天(ダウンロード用)")
    except Exception as exc:
        raise CouponStatementsError(
            "CSVダウンロード用のID/パスワードを取得できません。"
            f"{_CSV_USER_ENV} / {_CSV_PASSWORD_ENV} を設定してください。（{exc}）",
            state="AUTH_REQUIRED_MANUAL",
        ) from exc
    if credential is None:
        raise CouponStatementsError(
            "CSVダウンロード用のID/パスワードが見つかりません。"
            f"{_CSV_USER_ENV} / {_CSV_PASSWORD_ENV} を設定してください。",
            state="AUTH_REQUIRED_MANUAL",
        )
    return credential


def ensure_rakuten_login_env() -> list[str]:
    """R-Login・楽天会員の env が未設定なら認証情報Excelから補う。

    手順書どおり認証は3種必要だが、共有ログイン処理
    （access_analytics_rakuten._attempt_rakuten_login）は env しか読まない。
    ユーザーの既存資産（ID・PW.xlsx）だけで動く PC もあるため、
    **未設定のキーに限り**プロセス環境へ補完する（既存の env は上書きしない）。
    値はログ・戻り値へ出さず、補完できたキー名だけを返す。

    副作用の範囲: `os.environ` はプロセス全体で共有されるため、補完したキーは
    同じプロセスで動く他カード（アクセス解析・請求明細など楽天ログインを使うもの）
    からも見える。補完値は同じ ID・PW.xlsx 由来なので実害は無いが、
    後始末までしたい場合は下の `rakuten_login_env()`（with を抜けたら元に戻す）を使う。
    ジョブ実行経路（download_coupon_statements）は既にそちらを使っている。
    """

    filled: list[str] = []
    missing = [
        (label, id_env, pw_env)
        for label, id_env, pw_env in _RAKUTEN_ENV_FALLBACK
        if not os.environ.get(id_env, "").strip() or not os.environ.get(pw_env, "").strip()
    ]
    if not missing:
        return filled
    try:
        from portal_app.services.credentials import load_site_credential
    except Exception:
        return filled
    for label, id_env, pw_env in missing:
        try:
            credential = load_site_credential(label)
        except Exception:
            continue
        if credential is None:
            continue
        login_id, password = credential
        # 空文字で定義済みのキーもあるため setdefault ではなく空判定で入れる。
        if not os.environ.get(id_env, "").strip():
            os.environ[id_env] = login_id
        if not os.environ.get(pw_env, "").strip():
            os.environ[pw_env] = password
        filled.append(id_env)
    return filled


@contextmanager
def rakuten_login_env() -> Iterator[list[str]]:
    """`ensure_rakuten_login_env()` の補完を with ブロック内だけに閉じ込める。

    プロセス全体の os.environ を書き換える副作用が、ジョブ終了後まで他カードへ
    残らないようにする（補完したキーだけを元の状態＝未設定/空へ戻す）。
    """

    keys = [env for _, id_env, pw_env in _RAKUTEN_ENV_FALLBACK for env in (id_env, pw_env)]
    before = {key: os.environ.get(key) for key in keys}
    filled: list[str] = []
    try:
        filled = ensure_rakuten_login_env()
        yield filled
    finally:
        for key, original in before.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original


async def _page_text(page) -> str:
    try:
        return await page.locator("body").inner_text()
    except Exception:
        return ""


async def _needs_login(page) -> bool:
    parsed = urlparse(page.url)
    if parsed.hostname != CSV_DOWNLOAD_HOST:
        return True
    text = await _page_text(page)
    return _AUTH_REQUIRED_MARKER in text


async def _open_csv_download_page(page) -> None:
    """受注データダウンロード画面を開く。

    手順書どおり「goto → `認証が必要です` を検出したら login() をやり直して再 goto、
    最大3回」。ログイン処理そのものは稼働実績のある共有実装を再利用する。
    """

    ensure_rakuten_login_env()
    timeout = nav_timeout_ms()
    last_error: Exception | None = None
    for _ in range(3):
        await page.goto(CSV_DOWNLOAD_URL, wait_until="domcontentloaded", timeout=timeout)
        if not await _needs_login(page):
            try:
                await page.wait_for_selector('select[name="fromYmd"]', timeout=timeout)
                return
            except PlaywrightTimeoutError as exc:
                last_error = exc
        try:
            await _attempt_rakuten_login(page, target_url=CSV_DOWNLOAD_URL)
        except AccessAnalyticsError as exc:
            raise CouponStatementsError(str(exc), state=exc.state) from exc
    if last_error is not None:
        raise CouponStatementsError(
            "受注データダウンロード画面の期間指定欄が表示されませんでした。",
            state="PAGE_CONTRACT_CHANGED",
        ) from last_error
    raise CouponStatementsError(
        "楽天RMSのログインが確立できませんでした（認証が必要ですの表示が続いています）。",
        state="NEEDS_LOGIN",
    )


async def _select_order_date_type(page, warnings: list[str]) -> None:
    """日付種別を「注文日（時）」にする。

    日別売上フローは発送日（input#r06）を選ぶが、クーポン明細は注文日時基準なので
    r06 は選ばない。id は画面改修で変わり得るため、ラベル文言（注文日）で特定する。
    RMSのラジオは装飾 span に覆われて check() が弾かれることがあるため、
    通常 check → ラベルクリック → force check の順でフォールバックする。
    """

    radios = page.locator('input[type="radio"][name="dateType"]')
    count = await radios.count()
    for index in range(count):
        radio = radios.nth(index)
        element_id = await radio.get_attribute("id")
        if element_id == SHIPPING_DATE_RADIO_ID:
            continue  # 発送日。クーポン明細では使わない
        label = None
        label_text = ""
        if element_id:
            label = page.locator(f'label[for="{element_id}"]')
            if await label.count() > 0:
                label_text = (await label.first.inner_text()).strip()
            else:
                label = None
        if not label_text:
            try:
                label_text = (await radio.locator("xpath=..").inner_text()).strip()
            except Exception:
                label_text = ""
        if ORDER_DATE_LABEL_KEYWORD not in label_text:
            continue
        try:
            await radio.check(timeout=5_000)
            return
        except PlaywrightTimeoutError:
            pass
        if label is not None:
            await label.first.click()
            if await radio.is_checked():
                return
        await radio.check(force=True)
        return
    if count == 0:
        warnings.append(
            "日付種別のラジオボタンが見つからないため、画面の既定値のまま実行しました。"
        )
        return
    raise CouponStatementsError(
        "日付種別「注文日」を選択できませんでした。画面構成が変わった可能性があります。",
        state="PAGE_CONTRACT_CHANGED",
    )


async def _option_values(page, selector: str) -> list[str]:
    locator = page.locator(f"{selector} option")
    if await locator.count() == 0:
        return []
    return await locator.evaluate_all("(options) => options.map((option) => option.value)")


async def _select_period(page, period: CouponPeriod, warnings: list[str]) -> None:
    for selector, value, label in (
        ('select[name="fromYmd"]', period.start_text, "開始日"),
        ('select[name="toYmd"]', period.end_text, "終了日"),
    ):
        values = await _option_values(page, selector)
        if not values:
            raise CouponStatementsError(
                f"期間指定の{label}セレクトが見つかりません。",
                state="PAGE_CONTRACT_CHANGED",
            )
        if value not in values:
            raise CouponStatementsError(
                f"期間指定の{label}に {value} が選択肢としてありません"
                "（RMSの選択可能期間を過ぎている可能性があります）。",
                state="PERIOD_NOT_AVAILABLE",
            )
        await page.select_option(selector, value=value)

    # 時刻セレクトを 00:00:00 / 23:59:59 に寄せる。
    # 手順書（日別売上）は時刻欄に触れていない＝画面に無い可能性が高いが、
    # 「あるのに選べなかった」を黙って通すと期間が欠けるため、実在有無で扱いを分ける。
    found_any = False
    for names, candidates, label in (
        (("fromHour", "fromH"), ("0", "00"), "開始時"),
        (("fromMinute", "fromMin", "fromM"), ("0", "00"), "開始分"),
        (("fromSecond", "fromSec", "fromS"), ("0", "00"), "開始秒"),
        (("toHour", "toH"), ("23",), "終了時"),
        (("toMinute", "toMin", "toM"), ("59",), "終了分"),
        (("toSecond", "toSec", "toS"), ("59",), "終了秒"),
    ):
        exists, selected = await _select_time(page, names, candidates)
        found_any = found_any or exists
        if exists and not selected:
            warnings.append(
                f"期間指定の{label}セレクトに希望の値（{'/'.join(candidates)}）が無く、"
                "画面の既定値のまま実行しました。"
            )
    if not found_any:
        warnings.append(
            "期間指定に時刻セレクトが無いため、画面既定の時刻"
            "（月初 00:00:00 〜 月末 23:59:59）で実行しました。"
        )


async def _select_time(
    page, names: tuple[str, ...], candidates: tuple[str, ...]
) -> tuple[bool, bool]:
    """時刻セレクトを選択する。戻り値は (セレクトが実在したか, 選択できたか)。"""

    for name in names:
        selector = f'select[name="{name}"]'
        values = await _option_values(page, selector)
        if not values:
            continue
        for candidate in candidates:
            if candidate in values:
                await page.select_option(selector, value=candidate)
                return True, True
        return True, False
    return False, False


async def _select_coupon_template(page, warnings: list[str]) -> None:
    """出力テンプレートを「クーポン」にする（value ではなくラベルを正本にする）。"""

    selector = 'select[name="templateId"]'
    options = page.locator(f"{selector} option")
    count = await options.count()
    if count == 0:
        raise CouponStatementsError(
            "出力テンプレートのセレクトが見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    pairs = await options.evaluate_all(
        "(options) => options.map((option) => [option.value, (option.textContent || '').trim()])"
    )
    # 既定は観測済みの value="3"。ただしラベルが「クーポン」でなければ採用せず、
    # ラベル一致の option を探し直す（select は表示テキストでなく value で選ぶ）。
    documented = next(
        (
            value
            for value, text in pairs
            if value == COUPON_TEMPLATE_ID and COUPON_TEMPLATE_LABEL in text
        ),
        None,
    )
    chosen = documented or next(
        (value for value, text in pairs if COUPON_TEMPLATE_LABEL in text),
        None,
    )
    if chosen is None:
        available = "/".join(text for _, text in pairs if text)
        raise CouponStatementsError(
            f"出力テンプレート「{COUPON_TEMPLATE_LABEL}」が選択肢にありません（{available}）。",
            state="PAGE_CONTRACT_CHANGED",
        )
    if chosen != COUPON_TEMPLATE_ID:
        warnings.append(
            f"出力テンプレート「{COUPON_TEMPLATE_LABEL}」の value が {chosen} でした"
            f"（従来 {COUPON_TEMPLATE_ID}）。ラベル一致で選択しています。"
        )
    await page.select_option(selector, value=chosen)

    # hidden の templateName は通常サイト側 JS が同期するが、未同期でも成立させる。
    hidden = page.locator('input[name="templateName"]')
    if await hidden.count() > 0:
        label = next((text for value, text in pairs if value == chosen), COUPON_TEMPLATE_LABEL)
        await hidden.first.evaluate("(element, value) => { element.value = value; }", label)


async def _wait_for_download_form(
    page, *, deadline: float, cancel_check: CancelCheck | None = None
) -> bool:
    """「処理中」が終わり、CSVダウンロード認証欄（input#user）が出るまで待つ。

    手順書は「input#user[name="user"] の出現を最大120秒待機」だが、クーポンは
    大量データで10分以上かかることがあるため上限だけ延ばしている
    （既定 KURIMA_COUPON_DATA_WAIT_SECONDS = 1800 秒）。
    ページはリロードしない（データ作成結果の表示状態を壊さないため）。
    データ0件を検知したら False を返す。
    待機はこのカードで一番長いので、3秒ごとに「実行を中止」を確認する。
    """

    user_input = page.locator('input#user[name="user"], input[name="user"]')
    loop = asyncio.get_running_loop()
    while loop.time() < deadline:
        raise_if_cancelled(cancel_check)
        if await user_input.count() > 0 and await user_input.first.is_visible():
            return True
        text = await _page_text(page)
        if any(marker in text for marker in _NO_DATA_MARKERS):
            return False
        await asyncio.sleep(3)
    raise CouponStatementsError(
        "データ作成が既定時間内に終わりませんでした。"
        "RMSの「ダウンロード利用履歴」から手動で取得するか、"
        f"{_DATA_WAIT_SECONDS_ENV} を延ばして再実行してください。",
        state="DATA_CREATION_TIMEOUT",
    )


def _download_snapshot(directory: Path) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() in {".tmp", ".crdownload", ".part"}:
            continue
        stat = path.stat()
        snapshot[path] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


async def _wait_stable_file(directory: Path, before: dict[Path, tuple[int, int]]) -> Path:
    deadline = asyncio.get_running_loop().time() + 90
    sizes: dict[Path, int] = {}
    stable: dict[Path, int] = {}
    while asyncio.get_running_loop().time() < deadline:
        current = _download_snapshot(directory)
        candidates = [path for path, stamp in current.items() if before.get(path) != stamp]
        if len(candidates) > 1:
            raise CouponStatementsError(
                "ダウンロード候補が複数あり、安全に1件へ確定できません。",
                state="AMBIGUOUS_DOWNLOAD",
            )
        if len(candidates) == 1:
            candidate = candidates[0]
            size = current[candidate][0]
            stable[candidate] = (
                stable.get(candidate, 0) + 1 if sizes.get(candidate) == size and size > 0 else 0
            )
            sizes[candidate] = size
            if stable[candidate] >= 2:
                return candidate
        await asyncio.sleep(0.5)
    raise CouponStatementsError(
        "クーポン明細CSVを回収できませんでした。",
        state="DOWNLOAD_FAILED",
    )


async def _capture_download(page, locator, *, directory: Path, destination: Path) -> Path:
    before = _download_snapshot(directory)
    timeout = download_timeout_ms(120_000)
    try:
        async with page.expect_download(timeout=timeout) as download_info:
            await locator.click()
        download = await download_info.value
        failure = await download.failure()
        if failure:
            raise CouponStatementsError(
                f"クーポン明細CSVのダウンロードに失敗しました: {failure}",
                state="DOWNLOAD_FAILED",
            )
        await download.save_as(str(destination))
        return destination
    except PlaywrightTimeoutError:
        fallback = await _wait_stable_file(directory, before)
        if fallback.resolve() != destination.resolve():
            shutil.move(str(fallback), destination)
        return destination


async def _fill_download_credentials(page) -> bool:
    """CSV DL 直前の第3認証（input#user + downloadPassword）を埋める。

    欄が無ければ何もしない（ダウンロード利用履歴の画面では出ないことがある）。
    """

    user_input = page.locator('input#user[name="user"], input[name="user"]')
    if await user_input.count() == 0:
        return False
    user, password = load_csv_download_credential()
    await user_input.first.fill(user)
    password_input = page.locator(
        'input[name="downloadPassword"], input#password, input[type="password"]'
    )
    if await password_input.count() > 0:
        await password_input.first.fill(password)
    return True


async def _download_csv(page, *, batch_dir: Path, destination: Path) -> Path:
    if not await _fill_download_credentials(page):
        raise CouponStatementsError(
            "CSVダウンロードの認証欄（ユーザーID）が見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    button = page.locator("#downloadBtn")
    if await button.count() == 0:
        button = page.get_by_role("button", name=re.compile("ダウンロード"))
    if await button.count() == 0:
        raise CouponStatementsError(
            "「ダウンロードする」ボタンが見つかりません。",
            state="PAGE_CONTRACT_CHANGED",
        )
    return await _capture_download(
        page, button.first, directory=batch_dir, destination=destination
    )


# --------------------------------------------------------------------------
# ダウンロード利用履歴からの回収（データ作成待ちが上限に達したときの救済経路）
# --------------------------------------------------------------------------
def _history_row_markers(period: CouponPeriod) -> tuple[str, ...]:
    """履歴行が対象期間のものかを見分ける手掛かり（表記ゆれを吸収する）。"""

    markers: list[str] = []
    for day in (period.start_date, period.end_date):
        markers.append(day.isoformat())
        markers.append(f"{day.year}/{day.month:02d}/{day.day:02d}")
        markers.append(f"{day.year}/{day.month}/{day.day}")
        markers.append(f"{day.year}年{day.month}月{day.day}日")
    return tuple(dict.fromkeys(markers))


async def _open_download_history(page) -> bool:
    """「ダウンロード利用履歴」画面を開く。開けたら True。

    直URLは公開仕様が無いため、既定は現在の画面のリンク文言からたどる
    （必要なら KURIMA_COUPON_HISTORY_URL で直接指定できる）。
    """

    override = os.environ.get(_HISTORY_URL_ENV, "").strip()
    if override:
        await page.goto(override, wait_until="domcontentloaded", timeout=nav_timeout_ms())
        return True
    link = page.get_by_role("link", name=re.compile(_DOWNLOAD_HISTORY_LABEL))
    if await link.count() == 0:
        link = page.locator(f'a:has-text("{_DOWNLOAD_HISTORY_LABEL}")')
    if await link.count() == 0:
        return False
    await link.first.click()
    await page.wait_for_load_state("domcontentloaded", timeout=nav_timeout_ms())
    return True


async def _find_history_row(page, period: CouponPeriod):
    """対象期間（または「クーポン」）に該当する履歴行のうち、最も新しいものを返す。

    RMSの履歴は新しい申込が上に来るため、上から見て最初の一致を採用する
    （クリックポストの「最新申込優先」と同じ考え方）。
    """

    markers = _history_row_markers(period)
    rows = page.locator("table tr")
    count = await rows.count()
    fallback = None
    for index in range(min(count, 100)):
        row = rows.nth(index)
        try:
            text = (await row.inner_text()).strip()
        except Exception:
            continue
        if not text or _HISTORY_DOWNLOAD_LABEL not in text:
            continue
        if any(marker in text for marker in markers):
            return row
        if fallback is None and COUPON_TEMPLATE_LABEL in text:
            fallback = row
    return fallback


async def _download_from_history(
    page, *, period: CouponPeriod, batch_dir: Path, destination: Path
) -> Path | None:
    """作成済みデータを「ダウンロード利用履歴」から回収する。失敗しても例外にしない。"""

    try:
        if not await _open_download_history(page):
            return None
        row = await _find_history_row(page, period)
        if row is None:
            return None
        await _fill_download_credentials(page)
        target = row.get_by_role("link", name=re.compile(_HISTORY_DOWNLOAD_LABEL))
        if await target.count() == 0:
            target = row.locator(
                f'a:has-text("{_HISTORY_DOWNLOAD_LABEL}"), '
                f'button:has-text("{_HISTORY_DOWNLOAD_LABEL}")'
            )
        if await target.count() == 0:
            return None
        return await _capture_download(
            page, target.first, directory=batch_dir, destination=destination
        )
    except CouponStatementsError:
        return None
    except Exception:
        return None


def _count_csv_rows(path: Path) -> int | None:
    """明細行数（ヘッダー除く）をざっくり数える。文字コードは CP932 想定。"""

    try:
        text = path.read_bytes().decode("cp932", errors="replace")
    except OSError:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    return max(0, len(lines) - 1) if lines else 0


def validate_coupon_csv_bytes(data: bytes) -> str | None:
    """ダウンロード物がクーポン明細CSVとして妥当かを判定する。妥当なら None。

    認証切れやエラー時に HTML が落ちてくることがあり、それを
    `{YYMM}_coupon.csv` として保存すると前月の正常なCSVを壊す。保存の前に
    「cp932 で読めるか」「HTMLではないか」「ヘッダー行に受注データの列名があるか」
    だけを見る（列数や中身の検査まではしない＝仕様変更に過敏にしない）。
    戻り値は利用者に見せる日本語の理由。
    """

    if not data.strip():
        return "中身が空です"
    head = data[:_CSV_VALIDATION_BYTES]
    lowered = head[:2048].lower()
    if any(marker.encode("ascii") in lowered for marker in _HTML_MARKERS):
        return "CSVではなくHTML（エラー画面など）が返っています"
    # 末尾でマルチバイト文字が切れる可能性があるため、最後の改行までで判定する。
    boundary = head.rfind(b"\n")
    body = head[: boundary + 1] if boundary > 0 else head
    try:
        text = body.decode("cp932")
    except UnicodeDecodeError:
        return "Shift-JIS(cp932)として読めません"
    header = next((line for line in text.splitlines() if line.strip()), "")
    if not any(keyword in header for keyword in _CSV_HEADER_KEYWORDS):
        return "先頭行に受注データの列名（注文番号・ステータス等）がありません"
    return None


def _quarantine_download(temporary: Path, period: CouponPeriod) -> Path | None:
    """検証に落ちたダウンロード物を隔離先へ退避する（調査用。保存先は汚さない）。"""

    try:
        quarantine_dir = APP_ROOT / "data" / "coupon_statements" / _QUARANTINE_DIRNAME
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        kept = quarantine_dir / f"{period.yymm}_coupon_{uuid4().hex[:8]}.bin"
        shutil.move(str(temporary), str(kept))
        return kept
    except OSError:
        return None


def _commit_csv(
    temporary: Path, *, paths: CouponPaths, period: CouponPeriod
) -> tuple[Path, list[str]]:
    """検証に通ったときだけ `{YYMM}_coupon.csv` を置き換える。"""

    warnings: list[str] = []
    try:
        head = temporary.read_bytes()[:_CSV_VALIDATION_BYTES]
    except OSError as exc:
        raise CouponStatementsError(
            f"ダウンロードしたファイルを読めませんでした（{exc}）。",
            state="DOWNLOAD_FAILED",
        ) from exc
    reason = validate_coupon_csv_bytes(head)
    if reason is not None:
        kept = _quarantine_download(temporary, period)
        message = (
            f"ダウンロードした内容をクーポン明細CSVとして保存できません（{reason}）。"
            f"既存の {period.csv_name} はそのまま保護しています。"
        )
        if kept is not None:
            message += f"取得したファイルは {kept} に残しました。"
        raise CouponStatementsError(message, state="INVALID_DOWNLOAD")

    paths.csv_dir.mkdir(parents=True, exist_ok=True)
    destination = paths.csv_dir / period.csv_name
    if destination.exists():
        warnings.append(f"{destination.name} が既にあったため、最新の内容で置き換えました。")
    replace_file(temporary, destination, label="保存先のCSV")
    return destination, warnings


def _read_saved_result(period: CouponPeriod) -> CouponStatementsResult:
    """dry-run: 実行せず、保存済みCSV・複製済みブックの有無だけを確認する。"""

    warnings: list[str] = []
    try:
        paths = find_coupon_paths()
    except FileNotFoundError as exc:
        raise CouponStatementsError(str(exc), state="PATH_NOT_FOUND") from exc
    csv_path = paths.csv_dir / period.csv_name
    workbook = find_existing_workbook(paths.root, period.month)
    if not csv_path.is_file():
        warnings.append(f"{period.csv_name} はまだ保存されていません。")
    if workbook is None:
        warnings.append(f"{period.workbook_name} はまだ作成されていません。")
    return CouponStatementsResult(
        executed=False,
        period=period,
        csv_path=csv_path if csv_path.is_file() else None,
        csv_bytes=csv_path.stat().st_size if csv_path.is_file() else None,
        csv_rows=_count_csv_rows(csv_path) if csv_path.is_file() else None,
        workbook_path=workbook,
        workbook_created=False,
        skipped_reason="dry-run: 保存済みファイルの有無のみ確認しました。",
        warnings=tuple(warnings),
    )


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------
async def download_coupon_statements(
    *,
    execute: bool,
    today: date | None = None,
    copy_workbook: bool = True,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    cancel_check: CancelCheck | None = None,
) -> CouponStatementsResult:
    """前月分のクーポン明細CSVを取得し、テンプレート xlsm を複製する。

    cancel_check: 「実行を中止」が押されたかを返す関数。長い待機ループの中から
    定期的に呼ばれ、True になった時点で JobCancelled を送出して安全に抜ける。
    """

    period = resolve_period(today)
    if not execute:
        return _read_saved_result(period)

    try:
        paths = find_coupon_paths()
    except FileNotFoundError as exc:
        raise CouponStatementsError(str(exc), state="PATH_NOT_FOUND") from exc

    warnings: list[str] = []
    staging_root = APP_ROOT / "data" / "coupon_statements" / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    batch_dir = staging_root / uuid4().hex
    batch_dir.mkdir(parents=True, exist_ok=False)
    profile_dir = _profile_dir()
    profile_dir.mkdir(parents=True, exist_ok=True)

    csv_path: Path | None = None
    csv_rows: int | None = None
    skipped_reason: str | None = None

    try:
        # 楽天ログイン用の env 補完はこの with の中だけ（終了後にプロセス環境を元へ戻す）。
        with rakuten_login_env():
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    accept_downloads=True,
                    downloads_path=str(batch_dir),
                    headless=_headless_value(headless),
                    locale="ja-JP",
                    slow_mo=max(0, int(slow_mo_ms)),
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    raise_if_cancelled(cancel_check)
                    await _open_csv_download_page(page)
                    raise_if_cancelled(cancel_check)
                    await _select_order_date_type(page, warnings)
                    await _select_period(page, period, warnings)
                    await _select_coupon_template(page, warnings)

                    create_button = page.locator("#dataCreateBtn")
                    if await create_button.count() == 0:
                        raise CouponStatementsError(
                            "「データを作成する」ボタンが見つかりません。",
                            state="PAGE_CONTRACT_CHANGED",
                        )
                    raise_if_cancelled(cancel_check)
                    await create_button.first.click()
                    await page.wait_for_timeout(3_000)

                    downloaded: Path | None = None
                    text = await _page_text(page)
                    if any(marker in text for marker in _NO_DATA_MARKERS):
                        skipped_reason = f"{period.label}のクーポン明細は0件でした。"
                    else:
                        deadline = asyncio.get_running_loop().time() + _data_wait_seconds()
                        try:
                            has_form = await _wait_for_download_form(
                                page, deadline=deadline, cancel_check=cancel_check
                            )
                        except CouponStatementsError as exc:
                            if exc.state != "DATA_CREATION_TIMEOUT":
                                raise
                            # 作成そのものは走り続けている可能性が高い。
                            # task 明記の「ダウンロード利用履歴」から回収を試みる。
                            downloaded = await _download_from_history(
                                page,
                                period=period,
                                batch_dir=batch_dir,
                                destination=batch_dir / f"{uuid4().hex}.csv",
                            )
                            if downloaded is None:
                                raise
                            warnings.append(
                                "データ作成の待機が上限に達したため、"
                                "「ダウンロード利用履歴」から取得しました。"
                            )
                        else:
                            if not has_form:
                                skipped_reason = f"{period.label}のクーポン明細は0件でした。"
                            else:
                                raise_if_cancelled(cancel_check)
                                downloaded = await _download_csv(
                                    page,
                                    batch_dir=batch_dir,
                                    destination=batch_dir / f"{uuid4().hex}.csv",
                                )
                    if downloaded is not None:
                        csv_path, commit_warnings = _commit_csv(
                            downloaded, paths=paths, period=period
                        )
                        warnings.extend(commit_warnings)
                        csv_rows = _count_csv_rows(csv_path)
                finally:
                    await context.close()
    except CouponStatementsError:
        raise
    except PlaywrightTimeoutError as exc:
        raise CouponStatementsError(
            "楽天RMS画面の応答待ちがタイムアウトしました。",
            state="PAGE_CONTRACT_CHANGED",
        ) from exc
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)

    workbook_path: Path | None = None
    workbook_created = False
    if copy_workbook:
        raise_if_cancelled(cancel_check)
        workbook_path, workbook_created, copy_warnings = copy_coupon_workbook(paths, period)
        warnings.extend(copy_warnings)

    return CouponStatementsResult(
        executed=True,
        period=period,
        csv_path=csv_path,
        csv_bytes=csv_path.stat().st_size if csv_path and csv_path.is_file() else None,
        csv_rows=csv_rows,
        workbook_path=workbook_path,
        workbook_created=workbook_created,
        skipped_reason=skipped_reason,
        warnings=tuple(warnings),
    )


def download_coupon_statements_sync(
    *,
    execute: bool,
    today: date | None = None,
    copy_workbook: bool = True,
    headless: bool | None = None,
    slow_mo_ms: int = 0,
    cancel_check: CancelCheck | None = None,
) -> CouponStatementsResult:
    return asyncio.run(
        download_coupon_statements(
            execute=execute,
            today=today,
            copy_workbook=copy_workbook,
            headless=headless,
            slow_mo_ms=slow_mo_ms,
            cancel_check=cancel_check,
        )
    )


def read_coupon_preview(today: date | None = None) -> dict[str, object]:
    """画面表示用に、前月分の保存状況をまとめる（ブラウザは起動しない）。"""

    period = resolve_period(today)
    paths = find_coupon_paths()
    csv_path = paths.csv_dir / period.csv_name
    workbook = find_existing_workbook(paths.root, period.month)
    source_cells: dict[str, str] = {}
    if workbook is not None:
        try:
            source_cells = read_workbook_source_cells(workbook)
        except Exception:
            source_cells = {}
    return {
        "period_label": period.label,
        "period_range": f"{period.start_text} 00:00:00 〜 {period.end_text} 23:59:59",
        "csv_name": period.csv_name,
        "csv_dir": str(paths.csv_dir),
        "csv_exists": csv_path.is_file(),
        "csv_bytes": csv_path.stat().st_size if csv_path.is_file() else None,
        "csv_rows": _count_csv_rows(csv_path) if csv_path.is_file() else None,
        "csv_fetched_at": (
            datetime.fromtimestamp(csv_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            if csv_path.is_file()
            else None
        ),
        "workbook_name": period.workbook_name,
        "workbook_dir": str(paths.root),
        "workbook_exists": workbook is not None,
        "workbook_actual_name": workbook.name if workbook is not None else None,
        "template_exists": paths.template_path.is_file(),
        "template_name": paths.template_path.name,
        "source_file_path": source_cells.get("filePath", ""),
        "source_file_name": source_cells.get("fileName", ""),
    }


def resolve_coupon_csv_path(today: date | None = None) -> Path | None:
    """保存済みCSVのダウンロード用パス解決（クーポンcsvフォルダ内のみ許可）。"""

    period = resolve_period(today)
    try:
        paths = find_coupon_paths()
    except FileNotFoundError:
        return None
    candidate = (paths.csv_dir / period.csv_name).resolve()
    try:
        candidate.relative_to(paths.csv_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


__all__ = [
    "COUPON_TEMPLATE_ID",
    "COUPON_TEMPLATE_LABEL",
    "CSV_DOWNLOAD_URL",
    "CancelCheck",
    "CouponPeriod",
    "CouponStatementsError",
    "CouponStatementsResult",
    "ORDER_DATE_LABEL_KEYWORD",
    "RAKUTEN_LOGIN_ENTRY_URL",
    "SHIPPING_DATE_RADIO_ID",
    "copy_coupon_workbook",
    "download_coupon_statements",
    "download_coupon_statements_sync",
    "ensure_rakuten_login_env",
    "find_existing_workbook",
    "previous_month",
    "rakuten_login_env",
    "raise_if_cancelled",
    "read_cell_text",
    "read_coupon_preview",
    "read_workbook_source_cells",
    "remove_calc_chain_override",
    "remove_calc_chain_relationship",
    "replace_cached_cell_value",
    "replace_file",
    "resolve_coupon_csv_path",
    "resolve_period",
    "set_static_cell_value",
    "stamp_workbook_source",
    "validate_coupon_csv_bytes",
    "workbook_name_variants",
]
