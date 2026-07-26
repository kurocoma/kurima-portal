"""共有 .env（SharePoint の kurimaportal-app/）読み込みのテスト（2026-07-26）。

認証情報を全PCで共用するため、共有フォルダの .env を
「OS環境変数 > .env.local > 共有 .env > ローカル .env」の優先順位で読む。
テストは KURIMA_SHARED_ENV_DIR で一時フォルダを指し、実際の
SharePoint フォルダや os.environ の実値には触れない。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.env import (
    SHARED_ENV_DIR_ENV,
    _shared_env_dir_candidates,
    load_shared_env_file,
)

_KEY = "KURIMA_TEST_SHARED_VALUE"


class LoadSharedEnvFileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.shared_dir = Path(self._tmp.name)

    def _patch_env(self, extra: dict[str, str] | None = None):
        env = {SHARED_ENV_DIR_ENV: str(self.shared_dir)}
        env.update(extra or {})
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        if _KEY not in env:
            os.environ.pop(_KEY, None)

    def test_loads_value_from_shared_dotenv(self):
        (self.shared_dir / ".env").write_text(f"{_KEY}=shared\n", encoding="utf-8")
        self._patch_env()
        loaded = load_shared_env_file()
        self.assertEqual(loaded, self.shared_dir / ".env")
        self.assertEqual(os.environ[_KEY], "shared")

    def test_existing_environ_wins_over_shared(self):
        """OS環境変数（と、それより先に読む .env.local）の値が共有より優先される。"""
        (self.shared_dir / ".env").write_text(f"{_KEY}=shared\n", encoding="utf-8")
        self._patch_env(extra={_KEY: "local"})
        load_shared_env_file()
        self.assertEqual(os.environ[_KEY], "local")

    def test_visible_filename_is_accepted(self):
        """SharePoint上で見やすい kurima-portal.env という名前でも読める。"""
        (self.shared_dir / "kurima-portal.env").write_text(f"{_KEY}=visible\n", encoding="utf-8")
        self._patch_env()
        loaded = load_shared_env_file()
        self.assertEqual(loaded, self.shared_dir / "kurima-portal.env")
        self.assertEqual(os.environ[_KEY], "visible")

    def test_missing_dir_is_noop(self):
        """共有未導入のPC（フォルダなし）では何もせずNoneを返す。"""
        env = {SHARED_ENV_DIR_ENV: str(self.shared_dir / "not-exists")}
        with mock.patch.dict(os.environ, env):
            self.assertIsNone(load_shared_env_file())

    def test_candidates_include_portal_root_kurimaportal_app(self):
        """既定探索はポータル同期フォルダ直下の kurimaportal-app を候補にする。"""
        with mock.patch.dict(
            os.environ,
            {SHARED_ENV_DIR_ENV: "", "KURIMA_PORTAL_ROOT": str(self.shared_dir)},
        ):
            candidates = _shared_env_dir_candidates()
        self.assertIn(self.shared_dir / "kurimaportal-app", candidates)


if __name__ == "__main__":
    unittest.main()
