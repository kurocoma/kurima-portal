"""既知エラーを「原因＋次にやること」の日本語対処ガイドへ変換する共通機構（U1）。

ジョブ失敗時、画面には ``Timeout 60000ms exceeded.`` のような生の例外文字列が
そのまま出るため、非エンジニアには次の一手が分からない。ここで例外型・
メッセージ中のキーワードから対処文（hint）を引き、進捗スナップショット経由で
各画面のエラー表示の下に添える。

- 変換できない未知のエラーは None を返す（画面は従来どおり生メッセージのみ）。
- ヤマトB2 のログイン失敗は yamato_b2_import.B2LoginError が state
  （still_on_login / needs_2fa / system_error / time_outside）を持つ。
  Playwright を含む重いモジュールを import しないよう、型名と属性で判定する。
"""

from __future__ import annotations


# B2LoginError.state → 対処文（既存の原因分類 B2_LOGIN_STATES に対応）
_B2_STATE_HINTS = {
    "still_on_login": (
        "ヤマトB2にログインできませんでした。.env の YAMATO_B2_LOGIN_ID / "
        "YAMATO_B2_PASSWORD を確認し、修正後にもう一度実行してください。"
    ),
    "needs_2fa": (
        "ヤマトB2がワンタイムパスワード（2段階認証）を要求しています。"
        "ブラウザ表示（画面表示モード）で実行し、認証を完了してから再実行してください。"
    ),
    "system_error": (
        "ヤマトB2側でシステムエラーが表示されました。時間をおいて再実行してください。"
        "続く場合はB2クラウドの障害・メンテナンス情報を確認してください。"
    ),
    "time_outside": (
        "ヤマトB2のサービス時間外です。サービス時間内にもう一度実行してください。"
    ),
}


def hint_for_exception(exc: BaseException | None) -> str | None:
    """例外オブジェクトから対処ガイド（日本語）を引く。未知のエラーは None。"""
    if exc is None:
        return None

    # ヤマトB2ログイン失敗: 既存の原因分類（state）別に対処を出す
    state = getattr(exc, "state", None)
    if type(exc).__name__ == "B2LoginError" and isinstance(state, str):
        hint = _B2_STATE_HINTS.get(state)
        if hint:
            return hint

    # Windows の worker スレッドで subprocess 非対応ループになった場合など
    if isinstance(exc, NotImplementedError):
        return (
            "サーバー内部のブラウザ起動方式の問題です。scripts\\restart.bat で"
            "サーバーを再起動してから、もう一度実行してください。"
        )

    # Playwright の TimeoutError も型名は "TimeoutError"（builtins.TimeoutError と同名）
    if isinstance(exc, TimeoutError) or type(exc).__name__ == "TimeoutError":
        return _TIMEOUT_HINT

    # 保存先・ファイル未検出（find_portal_paths / latest_order_csv 等）
    if isinstance(exc, FileNotFoundError):
        return _file_not_found_hint(str(exc))

    if isinstance(exc, UnicodeEncodeError):
        return _ENCODING_HINT

    return hint_for_message(str(exc))


def hint_for_message(text: str | None) -> str | None:
    """エラーメッセージ文字列から対処ガイド（日本語）を引く。未知のエラーは None。

    progress_jobs.fail() へ文字列だけが渡ってくる経路（worker が組み立てた
    失敗メッセージ等）のためのフォールバック。
    """
    if not text:
        return None

    # Playwright の時間切れ（例: "Timeout 60000ms exceeded."）
    if "Timeout" in text and "exceeded" in text:
        return _TIMEOUT_HINT

    # Playwright ブラウザ実体が無い（例: "Executable doesn't exist at ..."）
    if "Executable doesn't exist" in text or "playwright install" in text:
        return (
            "自動操作用のブラウザが見つかりません。コマンド "
            "`uv run playwright install chromium` を実行してから再実行してください。"
        )

    # 接続エラー（回線断・名前解決失敗・接続拒否）
    if any(
        marker in text
        for marker in ("net::ERR", "ECONNREFUSED", "getaddrinfo", "ConnectionError", "ERR_CONNECTION")
    ):
        return (
            "サイトへ接続できませんでした。インターネット回線と VPN・プロキシの状態を"
            "確認し、少し待ってから再実行してください。"
        )

    # 保存先（SharePoint 同期フォルダ）未検出
    if "同期フォルダを検出できませんでした" in text or "KURIMA_PORTAL_ROOT" in text:
        return _PORTAL_ROOT_HINT

    # 入力 CSV 等の未検出（例: "data で始まる受注明細 CSV が見つかりません"）
    if "見つかりません" in text:
        return (
            "必要なファイルが見つかりません。SharePoint の同期が完了しているかを確認し、"
            "無い場合は先に取得処理（NE取得など）を実行してください。"
        )

    # ログイン失敗（Next Engine 等。B2 は hint_for_exception 側で state 別に処理済み）
    if "ログイン" in text and ("失敗" in text or "できません" in text):
        return (
            "ログインに失敗しました。.env の認証情報（ID・パスワード）が正しいかを確認し、"
            "修正後にもう一度実行してください。"
        )

    # CP932 で出力できない文字（環境依存文字）
    if "cp932" in text.lower() or "codec can't encode" in text:
        return _ENCODING_HINT

    return None


def _file_not_found_hint(message: str) -> str:
    if "同期フォルダを検出できませんでした" in message or "KURIMA_PORTAL_ROOT" in message:
        return _PORTAL_ROOT_HINT
    return (
        "必要なファイルが見つかりません。SharePoint の同期が完了しているかを確認し、"
        "無い場合は先に取得処理（NE取得など）を実行してください。"
    )


_TIMEOUT_HINT = (
    "サイトの応答待ちで時間切れになりました。もう一度実行してください。"
    "繰り返し発生する場合は、回線状況と対象サイトの混雑・障害を確認してください。"
)

_PORTAL_ROOT_HINT = (
    "保存先フォルダが見つかりません。SharePoint「くりまポータル」の同期状態を確認するか、"
    ".env の KURIMA_PORTAL_ROOT に同期フォルダのパスを設定してください。"
)

_ENCODING_HINT = (
    "CSVに出力できない文字（環境依存文字）が含まれています。"
    "対象の受注データの文字を一般的な文字へ修正してから再実行してください。"
)
