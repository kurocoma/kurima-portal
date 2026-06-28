from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import openpyxl


DEFAULT_CREDENTIAL_PATH = (
    Path.home() / "開発案件" / "日別売上集計データダウンロード" / "docs" / "ID・PW.xlsx"
)


@dataclass(frozen=True)
class NextEngineCredential:
    login_id: str
    password: str


def load_next_engine_credential() -> NextEngineCredential:
    env_login_id = os.environ.get("NEXT_ENGINE_LOGIN_ID")
    env_password = os.environ.get("NEXT_ENGINE_PASSWORD")
    if env_login_id and env_password:
        return NextEngineCredential(login_id=env_login_id, password=env_password)

    credential_path = Path(
        os.environ.get("NEXT_ENGINE_CREDENTIAL_PATH", str(DEFAULT_CREDENTIAL_PATH))
    )
    return _load_from_workbook(credential_path)


def _load_from_workbook(path: Path) -> NextEngineCredential:
    if not path.exists():
        raise FileNotFoundError(
            "Next Engine の認証情報が見つかりません。"
            "NEXT_ENGINE_LOGIN_ID/NEXT_ENGINE_PASSWORD または "
            f"NEXT_ENGINE_CREDENTIAL_PATH を設定してください: {path}"
        )

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        for row in sheet.iter_rows(min_row=2, values_only=True):
            site_label = str(row[0] or "").strip()
            if site_label != "ネクストエンジン":
                continue

            login_id = str(row[1] or "").strip()
            password = str(row[2] or "").strip()
            if not login_id or not password:
                break
            return NextEngineCredential(login_id=login_id, password=password)
    finally:
        workbook.close()

    raise ValueError(f"認証情報ファイルにネクストエンジン行が見つかりません: {path}")
