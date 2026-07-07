"""動作設定の一元管理モジュール。

- ブラウザ操作タイムアウトの設定一元化（S5）:
  Playwright の goto / ログイン遷移待ち / expect_download のタイムアウトを
  env で上書きできるようにする。**env 未設定時は呼び出し元の現行ハードコード値を
  そのまま既定として使う**ため、挙動は従来と完全一致する（回線が遅い日だけ
  `.env` 1 行＋再起動で回避できるようにするのが目的）。
- LAN 公開時の簡易アクセス制御（O3）: 許可クライアントの判定関数。
- 設定画面 /settings（U6）と README（O4）のための設定キーカタログ。
"""

from __future__ import annotations

import ipaddress
import os

from portal_app.env import env_int

# --- S5: ブラウザ操作タイムアウト ---------------------------------------------
# 命名は docs/improvement-ideas.md S5 節に従う。既定はコード上の現行値
# （goto/ログイン系 60000ms・ダウンロード系はサイトごとに 60000〜180000ms）。
NAV_TIMEOUT_MS_ENV = "KURIMA_NAV_TIMEOUT_MS"
DOWNLOAD_TIMEOUT_MS_ENV = "KURIMA_DOWNLOAD_TIMEOUT_MS"
DEFAULT_NAV_TIMEOUT_MS = 60000
DEFAULT_DOWNLOAD_TIMEOUT_MS = 180000


def nav_timeout_ms(default: int = DEFAULT_NAV_TIMEOUT_MS) -> int:
    """画面遷移（goto / reload / ログイン後の load 待ち）のタイムアウト(ms)。

    呼び出しごとに env を読むため、`.env` 変更＋再起動で全サービスに一括反映される。
    0 以下・数値でない値は既定値（設定ミスでブラウザ操作を壊さない）。
    """
    return env_int(NAV_TIMEOUT_MS_ENV, default, minimum=1)


def download_timeout_ms(default: int = DEFAULT_DOWNLOAD_TIMEOUT_MS) -> int:
    """ファイルダウンロード（expect_download）のタイムアウト(ms)。

    サイトごとに現行既定が異なる（NE受注明細 90 秒・納品書PDF 180 秒など）ため、
    呼び出し元が現行値を default として渡す。env 設定時のみ全系統が一括で変わる。
    """
    return env_int(DOWNLOAD_TIMEOUT_MS_ENV, default, minimum=1)


# --- O3: LAN 公開時の簡易アクセス制御 -----------------------------------------
ALLOWED_CLIENTS_ENV = "KURIMA_ALLOWED_CLIENTS"


def allowed_client_rules() -> list[str]:
    """KURIMA_ALLOWED_CLIENTS（カンマ区切りの IP / CIDR / プレフィックス）を読む。

    空リスト＝未設定＝無制限（現行互換）。例: `192.168.1.10, 192.168.10.0/24, 10.0.`
    """
    raw = os.environ.get(ALLOWED_CLIENTS_ENV, "")
    return [token.strip() for token in raw.split(",") if token.strip()]


def client_allowed(host: str | None, rules: list[str] | None = None) -> bool:
    """接続元 host が許可対象なら True（O3）。

    - ルール未設定（env 空）→ 常に許可（現行互換）。
    - ループバック（127.0.0.1 / ::1）→ 常に許可。ホストPC自身が設定ミスで
      自分の画面から締め出されないためのフェイルセーフ。
    - ルールは 3 形式: 完全一致 IP / CIDR（`/`入り）/ 前方一致プレフィックス（`.`終わり）。
      不正な書式のルールはそのルールだけ無視する（1 個の typo で全員 403 にしない）。
    - host が取得できない接続は許可（uvicorn 経由では常に IP が入る）。
    """
    active_rules = allowed_client_rules() if rules is None else rules
    if not active_rules:
        return True
    if not host:
        return True

    try:
        client_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = ipaddress.ip_address(host)
    except ValueError:
        client_ip = None
    if client_ip is not None and client_ip.is_loopback:
        return True

    for rule in active_rules:
        if rule == host:
            return True
        if rule.endswith(".") and host.startswith(rule):
            return True
        if "/" in rule and client_ip is not None:
            try:
                if client_ip in ipaddress.ip_network(rule, strict=False):
                    return True
            except ValueError:
                continue
    return False


# --- U6: 設定画面 /settings 用の設定キーカタログ -------------------------------
# README「環境変数」表と同じ構成（O4: 実装とリファレンスの乖離をここで防ぐ）。
# secret=True のキーは画面で必ずマスク表示する。
SETTINGS_CATALOG: tuple[tuple[str, tuple[tuple[str, str, bool], ...]], ...] = (
    (
        "パス・基本設定",
        (
            ("KURIMA_PORTAL_ROOT", "ポータル同期フォルダ（未設定なら既定候補を自動探索）", False),
            ("PORTAL_ROOT", "KURIMA_PORTAL_ROOT の後方互換キー", False),
            ("KURIMA_MASTER_BOOK", "商品管理シートの個別上書き", False),
            ("KURIMA_ORDER_CSV_DIR", "受注明細フォルダの個別上書き", False),
            ("KURIMA_TOOL_DIR", "ツールフォルダの個別上書き", False),
            ("KURIMA_PORT", "serve.ps1 / restart.bat の既定ポート（既定 8006）", False),
            ("KURIMA_ALLOWED_CLIENTS", "LAN公開時の許可クライアント（IP/CIDR/プレフィックス。未設定で無制限）", False),
        ),
    ),
    (
        "ログ関連",
        (
            ("KURIMA_LOG_DIR", "実行ログ・エラーログの出力先上書き", False),
            ("KURIMA_LOG_MAX_MB", "共有ログのローテーション上限MB（既定 5）", False),
            ("KURIMA_LOG_BACKUP_COUNT", "ローテーション保持世代数（既定 3）", False),
            ("KURIMA_LOG_SUFFIX", "共有ログファイル名のPC識別サフィックス上書き", False),
            ("KURIMA_LOG_RETENTION_DAYS", "logs/ の保持日数（既定 30。0 以下で無効）", False),
            ("KURIMA_JOB_HISTORY_MAX_LINES", "実行履歴 history.jsonl の保持行数（既定 2000）", False),
        ),
    ),
    (
        "ブラウザ操作タイムアウト",
        (
            (NAV_TIMEOUT_MS_ENV, "画面遷移・ログイン待ちのタイムアウトms（未設定なら 60000）", False),
            (DOWNLOAD_TIMEOUT_MS_ENV, "CSV/PDFダウンロード待ちのタイムアウトms（未設定ならサイト別 60000〜180000）", False),
        ),
    ),
    (
        "Next Engine",
        (
            ("NEXT_ENGINE_LOGIN_ID", "Next Engine ログインID", True),
            ("NEXT_ENGINE_PASSWORD", "Next Engine パスワード", True),
            ("NEXT_ENGINE_CREDENTIAL_PATH", "認証情報Excelのパス", False),
            ("NEXT_ENGINE_HEADLESS", "NE操作ブラウザの非表示実行（既定 true）", False),
        ),
    ),
    (
        "ヤマトB2",
        (
            ("YAMATO_B2_LOGIN_ID", "ヤマトB2 ログインID", True),
            ("YAMATO_B2_PASSWORD", "ヤマトB2 パスワード", True),
            ("YAMATO_B2_CLASS_CODE", "法人ログインの分類コード", True),
            ("YAMATO_B2_PERSONAL_ID", "法人ログインの個人ID", True),
            ("YAMATO_B2_URL", "B2ログインURLの上書き", False),
            ("YAMATO_B2_STORAGE_STATE", "ログインセッション保存先の上書き", False),
            ("YAMATO_B2_HEADLESS", "B2操作ブラウザの非表示実行", False),
            ("KURIMA_B2_CHROME_PATH", "B2取込に使う実ブラウザのパス上書き", False),
            ("KURIMA_B2_CHROME_PROFILE", "B2専用ブラウザプロファイルの保存先", False),
            ("KURIMA_B2_CHROME_PORT", "B2実ブラウザの CDP ポート（既定 9333）", False),
            ("KURIMA_B2_OPEN_URL", "B2ブラウザ起動時に開くURLの上書き", False),
        ),
    ),
    (
        "クリックポスト",
        (
            ("CLICKPOST_YAHOO_LOGIN_ID", "クリックポスト（Yahoo! ID）ログインID", True),
            ("CLICKPOST_YAHOO_PASSWORD", "クリックポスト（Yahoo! ID）パスワード", True),
            ("CLICKPOST_SECURITYCODE", "Yahoo! ログインのセキュリティコード", True),
            ("CLICKPOST_HEADLESS", "クリックポスト操作ブラウザの非表示実行（既定 true）", False),
        ),
    ),
    (
        "出荷確定（遡り日数）",
        (
            ("KURIMA_SHIPMENT_BUYER_LOOKBACK_DAYS", "購入者データの遡り日数（既定 20）", False),
            ("KURIMA_SHIPMENT_CLICKPOST_LOOKBACK_DAYS", "クリックポスト送り状の遡り日数（既定 20）", False),
            ("KURIMA_SHIPMENT_LETTERPACK_LOOKBACK_DAYS", "レターパック伝票の遡り日数（既定 30）", False),
            ("KURIMA_SHIPMENT_YAMATO_LOOKBACK_DAYS", "ヤマト発行済データの遡り日数（既定 30）", False),
        ),
    ),
    (
        "ブラウザ実体（Playwright）",
        (
            ("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "使用ブラウザ実行ファイルの固定", False),
            ("PLAYWRIGHT_BROWSERS_PATH", "Playwright 同梱ブラウザの導入先", False),
        ),
    ),
)


def mask_secret(value: str) -> str:
    """秘密値の表示用マスク（先頭 2 文字＋***）。2 文字以下は全部マスク。"""
    if len(value) <= 2:
        return "***"
    return value[:2] + "***"


def settings_snapshot() -> list[dict[str, object]]:
    """/settings 画面用に、カタログ全キーの現在値（秘密はマスク）を集める（U6）。"""
    groups: list[dict[str, object]] = []
    for group_name, keys in SETTINGS_CATALOG:
        items = []
        for key, purpose, secret in keys:
            raw = os.environ.get(key)
            is_set = bool(raw)
            if not is_set:
                display = "未設定"
            elif secret:
                display = mask_secret(raw)
            else:
                display = raw
            items.append(
                {
                    "key": key,
                    "purpose": purpose,
                    "is_set": is_set,
                    "secret": secret,
                    "display": display,
                }
            )
        groups.append({"name": group_name, "items": items})
    return groups
