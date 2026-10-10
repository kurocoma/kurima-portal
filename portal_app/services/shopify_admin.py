"""Shopify Admin GraphQL の最小クライアント（標準ライブラリのみ）。

認証は Dev Dashboard の自社ストア用 custom app による Client Credentials Grant。
product-register（dev/product-register/webui/lib/shopify/auth.ts）と同じアプリ・同じ方式で、
トークンは約24時間有効・refresh_token なし → 期限60秒前に同じリクエストで取り直す。

必要スコープ: write_files（ファイルの差し替え）。read_themes（テーマ参照の確認）は任意。
テーマファイルの書き換え（write_themes）は使わない。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from threading import Lock

API_VERSION = "2026-01"
DEFAULT_SHOP = "kurima-okinawa"
REQUEST_TIMEOUT_SECONDS = 60


class ShopifyError(RuntimeError):
    """Shopify API の失敗。message は画面にそのまま出せる日本語。"""


class ShopifyAccessDenied(ShopifyError):
    """アプリにスコープが無いとき（ACCESS_DENIED）。"""


@dataclass(frozen=True)
class ShopifyConfig:
    shop: str
    client_id: str
    client_secret: str

    @property
    def admin_url(self) -> str:
        return f"https://admin.shopify.com/store/{self.shop}"


def shopify_config() -> ShopifyConfig | None:
    """環境変数から設定を読む。ID/シークレットが未設定なら None。"""
    shop = (os.environ.get("SHOPIFY_SHOP") or DEFAULT_SHOP).strip()
    shop = shop.removesuffix(".myshopify.com")
    client_id = (os.environ.get("SHOPIFY_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("SHOPIFY_CLIENT_SECRET") or "").strip()
    if not shop or not client_id or not client_secret:
        return None
    return ShopifyConfig(shop=shop, client_id=client_id, client_secret=client_secret)


_token_lock = Lock()
_token_cache: dict[str, tuple[str, float, tuple[str, ...]]] = {}


def _http(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str = "POST",
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise ShopifyError(f"Shopify に接続できませんでした（{exc.reason}）。") from exc


def access_token(cfg: ShopifyConfig, *, force: bool = False) -> tuple[str, tuple[str, ...]]:
    """(トークン, 付与スコープ) を返す。キャッシュが有効ならそれを使う。"""
    key = f"{cfg.shop}\n{cfg.client_id}"
    now = time.time()
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and not force and now < cached[1] - 60:
            return cached[0], cached[2]

    body = urllib.parse.urlencode(
        {
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "grant_type": "client_credentials",
        }
    ).encode()
    status, raw = _http(
        f"https://{cfg.shop}.myshopify.com/admin/oauth/access_token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        payload = {}
    token = payload.get("access_token")
    if not token:
        detail = payload.get("error_description") or payload.get("error") or f"HTTP {status}"
        raise ShopifyError(
            f"Shopify のアクセストークンを取得できませんでした（{detail}）。"
            "SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET と、アプリがストアにインストール済みかを確認してください。"
        )
    scopes = tuple(s.strip() for s in str(payload.get("scope") or "").split(",") if s.strip())
    expires_at = now + float(payload.get("expires_in") or 86399)
    with _token_lock:
        _token_cache[key] = (token, expires_at, scopes)
    return token, scopes


def graphql(cfg: ShopifyConfig, query: str, variables: dict | None = None) -> dict:
    """GraphQL を実行して data を返す。トップレベル errors は ShopifyError にする。"""
    for attempt in range(2):
        token, _ = access_token(cfg, force=attempt > 0)
        status, raw = _http(
            f"https://{cfg.shop}.myshopify.com/admin/api/{API_VERSION}/graphql.json",
            data=json.dumps({"query": query, "variables": variables or {}}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        )
        if status == 401 and attempt == 0:
            continue  # トークン失効 → 取り直して1回だけ再試行
        break
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError as exc:
        raise ShopifyError(f"Shopify の応答を読み取れませんでした（HTTP {status}）。") from exc
    errors = payload.get("errors")
    if errors:
        text = errors if isinstance(errors, str) else "; ".join(
            str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors
        )
        if "ACCESS_DENIED" in json.dumps(errors) or "access denied" in text.lower():
            raise ShopifyAccessDenied(f"Shopify アプリに権限がありません（{text}）。")
        raise ShopifyError(f"Shopify API エラー（{text}）。")
    if status >= 400:
        raise ShopifyError(f"Shopify API エラー（HTTP {status}）。")
    return payload.get("data") or {}


def user_errors_text(errors: list[dict] | None) -> str:
    return "; ".join(str(e.get("message")) for e in errors or [] if e.get("message"))


# --- ファイル（コンテンツ > ファイル） ---------------------------------------

STAGED_UPLOADS_CREATE = """
mutation stagedUploadsCreate($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}"""

FILE_CREATE = """
mutation fileCreate($files: [FileCreateInput!]!) {
  fileCreate(files: $files) {
    files { id fileStatus }
    userErrors { field message code }
  }
}"""

FILE_BY_ID = """
query fileById($id: ID!) {
  node(id: $id) {
    ... on File {
      id
      fileStatus
      updatedAt
      alt
      fileErrors { message }
    }
    ... on MediaImage { image { url width height } }
  }
}"""

FILES_BY_NAME = """
query filesByName($query: String!) {
  files(first: 5, query: $query, sortKey: UPDATED_AT, reverse: true) {
    nodes {
      id
      fileStatus
      updatedAt
      alt
      ... on MediaImage { image { url width height } }
      fileErrors { message }
    }
  }
}"""


@dataclass(frozen=True)
class StoreFile:
    id: str
    status: str
    updated_at: str
    url: str | None
    alt: str
    errors: tuple[str, ...]


def _store_file(node: dict) -> StoreFile:
    image = node.get("image") or {}
    return StoreFile(
        id=node.get("id", ""),
        status=node.get("fileStatus", ""),
        updated_at=node.get("updatedAt", ""),
        url=image.get("url") or None,
        alt=node.get("alt") or "",
        errors=tuple(e.get("message", "") for e in node.get("fileErrors") or []),
    )


def url_filename(url: str | None) -> str:
    """CDN URL からファイル名部分（?v= を除く）を取り出す。"""
    return (url or "").split("?")[0].rsplit("/", 1)[-1]


def find_file(cfg: ShopifyConfig, filename: str) -> StoreFile | None:
    """ファイル名が完全一致する最新のファイルを返す（無ければ None）。"""
    data = graphql(cfg, FILES_BY_NAME, {"query": f"filename:{filename}"})
    for node in (data.get("files") or {}).get("nodes") or []:
        found = _store_file(node)
        # 検索は部分一致もあり得るので、URL のファイル名で完全一致を確認する
        if found.url and url_filename(found.url) != filename:
            continue
        return found
    return None


def _multipart(fields: list[tuple[str, str]], filename: str, content: bytes, mime: str):
    boundary = f"----kurima{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    chunks.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
    )
    chunks.append(content)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def replace_image_file(cfg: ShopifyConfig, *, filename: str, jpeg: bytes, alt: str) -> str:
    """同じファイル名の画像を差し替える（無ければ新規作成）。

    stagedUploadsCreate → staged URL へ multipart POST → fileCreate(duplicateResolutionMode: REPLACE)。
    テーマは shopify://shop_images/<ファイル名> で参照しているため、名前が同じなら設定変更は不要。
    戻り値は登録されたファイルの ID（wait_until_ready に渡す）。
    """
    staged = graphql(
        cfg,
        STAGED_UPLOADS_CREATE,
        {
            "input": [
                {
                    "filename": filename,
                    "mimeType": "image/jpeg",
                    "httpMethod": "POST",
                    "resource": "IMAGE",
                    "fileSize": str(len(jpeg)),
                }
            ]
        },
    ).get("stagedUploadsCreate") or {}
    problem = user_errors_text(staged.get("userErrors"))
    if problem:
        raise ShopifyError(f"アップロード先の発行に失敗しました（{problem}）。")
    targets = staged.get("stagedTargets") or []
    if not targets or not targets[0].get("url"):
        raise ShopifyError("アップロード先の発行に失敗しました（アップロード先が返りませんでした）。")
    target = targets[0]

    body, content_type = _multipart(
        [(p["name"], p["value"]) for p in target.get("parameters") or []],
        filename,
        jpeg,
        "image/jpeg",
    )
    status, raw = _http(target["url"], data=body, headers={"Content-Type": content_type})
    if status >= 300:
        raise ShopifyError(
            f"画像の送信に失敗しました（HTTP {status}: {raw[:120].decode('utf-8', 'replace')}）。"
        )

    created = graphql(
        cfg,
        FILE_CREATE,
        {
            "files": [
                {
                    "originalSource": target["resourceUrl"],
                    "contentType": "IMAGE",
                    "filename": filename,
                    "duplicateResolutionMode": "REPLACE",
                    "alt": alt,
                }
            ]
        },
    ).get("fileCreate") or {}
    problem = user_errors_text(created.get("userErrors"))
    if problem:
        raise ShopifyError(f"{filename} の登録に失敗しました（{problem}）。")
    files = created.get("files") or []
    if not files or not files[0].get("id"):
        raise ShopifyError(f"{filename} の登録結果が返りませんでした。")
    return files[0]["id"]


def wait_until_ready(
    cfg: ShopifyConfig,
    file_id: str,
    filename: str,
    *,
    timeout_seconds: float = 90,
    interval_seconds: float = 3,
    sleep=time.sleep,
) -> StoreFile:
    """登録したファイルが READY になるまで待ち、ファイル名が変わっていないことを確かめる。

    REPLACE が効かずに別名（末尾に UUID 付き）で登録された場合、テーマは古い画像を
    見続けるので失敗として扱う。失敗・時間切れは ShopifyError。
    """
    deadline = time.monotonic() + timeout_seconds
    last: StoreFile | None = None
    while True:
        node = graphql(cfg, FILE_BY_ID, {"id": file_id}).get("node")
        last = _store_file(node) if node else None
        if last is not None:
            if last.status == "FAILED":
                detail = "; ".join(last.errors) or "理由不明"
                raise ShopifyError(f"{filename} の処理が Shopify 側で失敗しました（{detail}）。")
            if last.status == "READY":
                actual = url_filename(last.url)
                if actual and actual != filename:
                    raise ShopifyError(
                        f"{filename} が別名（{actual}）で登録されました。テーマの画像は差し替わっていません。"
                        "Shopify 管理画面の「コンテンツ > ファイル」で重複を確認してください。"
                    )
                return last
        if time.monotonic() >= deadline:
            state = f"状態 {last.status}" if last else "ファイルが見つかりません"
            raise ShopifyError(
                f"{filename} の反映確認が時間内に終わりませんでした（{state}）。"
                "数分後に画面の「Shopifyの状態」で確認してください。"
            )
        sleep(interval_seconds)


# --- テーマ（読み取りのみ） ---------------------------------------------------

MAIN_THEME_SETTINGS = """
query mainThemeSettings {
  themes(first: 1, roles: [MAIN]) {
    nodes {
      id
      name
      files(filenames: ["config/settings_data.json"], first: 1) {
        nodes { body { ... on OnlineStoreThemeFileBodyText { content } } }
      }
    }
  }
}"""


def read_main_theme_settings(cfg: ShopifyConfig) -> tuple[str, str]:
    """公開テーマの (テーマ名, settings_data.json の本文) を返す。read_themes が必要。"""
    data = graphql(cfg, MAIN_THEME_SETTINGS)
    nodes = (data.get("themes") or {}).get("nodes") or []
    if not nodes:
        raise ShopifyError("公開中のテーマが見つかりませんでした。")
    theme = nodes[0]
    files = (theme.get("files") or {}).get("nodes") or []
    content = ((files[0] if files else {}).get("body") or {}).get("content")
    if content is None:
        raise ShopifyError("テーマの settings_data.json を読み取れませんでした。")
    return theme.get("name", ""), content
