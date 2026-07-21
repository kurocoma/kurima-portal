"""保存済み結果の読み込み（execute=False）が batch_complete マーカーに汚染されない回帰テスト。

背景（2026-07-13 判明）:
楽天BillPayの `execute=False` が「実取得・実保存は成功しているのに取得済み文書0件」を返した。
原因は `billing_statements_rakuten._read_saved_result` の候補抽出が `category` で絞られておらず、
batch_complete マーカー（`issue_date=None` / `artifact_id=None`）が帳票候補へ混入していたこと。
その結果 `scope="latest"` の `max(str(record.get("issue_date", "")) ...)` が
`str(None) == "None"` を最大値として拾い（"N"=0x4E > "2"=0x32）、実在する帳票
（issue_date="2026-07-03"）が後段の等値フィルタから全て振り落とされて0件になっていた。

同じ「batch_complete マーカーを成果物レコードとして扱ってしまう」バグパターンが
他3モジュールにも無いことを、同形式の合成manifestで固定する。
実サイト・ネットワーク・実データには一切触れない（tmpディレクトリ上の合成CSVのみ）。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portal_app.services import access_analytics_rakuten as aa_rakuten  # noqa: E402
from portal_app.services import access_analytics_yahoo as aa_yahoo  # noqa: E402
from portal_app.services import billing_statements_rakuten as bs_rakuten  # noqa: E402
from portal_app.services import billing_statements_yahoo as bs_yahoo  # noqa: E402
from portal_app.services.access_analytics_rakuten import (  # noqa: E402
    EXPECTED_HEADER_28,
)
from portal_app.services.access_analytics_yahoo import (  # noqa: E402
    EXPECTED_PRODUCT_HEADER_14,
)
from portal_app.services.billing_statements_rakuten import (  # noqa: E402
    EXPECTED_SHOP_DETAIL_HEADER_17,
)
from portal_app.services.billing_statements_yahoo import (  # noqa: E402
    EXPECTED_BILLING_RECEIPT_HEADER_7,
)


def _cp932_crlf_bytes(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerows(rows)
    return buffer.getvalue().encode("cp932")


def _utf8_bom_bytes(rows: list[list[str]]) -> bytes:
    text = "\n".join(",".join(row) for row in rows) + "\n"
    return b"\xef\xbb\xbf" + text.encode("utf-8")


class _ManifestFixture(unittest.TestCase):
    """tmpディレクトリを保存ルートに差し替え、合成manifest＋合成CSVを組み立てる土台。"""

    env_key = ""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "raw").mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "manifest.jsonl"

        self._saved_env: dict[str, str | None] = {}
        # 保存ルートをtmpへ向ける。BillPayの期待値照合envは、開発者の実環境設定に
        # テスト結果が左右されないよう明示的に外す（未設定時は照合をスキップし警告を出す挙動）。
        overrides = {
            self.env_key: str(self.root),
            "KURIMA_BILLPAY_EXPECTED_COMPANY_ID": None,
            "KURIMA_BILLPAY_EXPECTED_SHOP_ID": None,
            "KURIMA_BILLPAY_EXPECTED_SHOP_URL": None,
        }
        for key, value in overrides.items():
            self._saved_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def tearDown(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def write_artifact(self, filename: str, data: bytes) -> tuple[str, str]:
        """raw/ へ成果物を置き、(relative_path, sha256) を返す。"""

        destination = self.root / "raw" / filename
        destination.write_bytes(data)
        return f"raw/{filename}", hashlib.sha256(data).hexdigest()

    def write_manifest(self, records: list[dict[str, object]]) -> None:
        with self.manifest_path.open("w", encoding="utf-8", newline="\n") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


class BillPayReadSavedResultTest(_ManifestFixture):
    """本命の回帰テスト: batch_complete マーカーが帳票を隠さないこと。"""

    env_key = "KURIMA_BILLING_STATEMENTS_DIR"

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            bs_rakuten, "_AUDIT_PATH", self.root / "audit" / "billpay.jsonl"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _shop_detail_bytes(self, *, issue_date: str = "2026/07/03") -> bytes:
        row = [
            issue_date, "S0001", "D0001", "shop001", "https://example.com/shop001",
            "テスト店舗", "10,000", "-", "0", "支払", "商品代金", "商品A",
            "2026/07/01", "2026/07/31", "10,000", "1,000", "10%",
        ]
        return _cp932_crlf_bytes([list(EXPECTED_SHOP_DETAIL_HEADER_17), row])

    def _document_record(
        self,
        *,
        issue_date: str,
        batch_id: str,
        artifact_id: str,
        relative_path: str,
        sha256: str,
    ) -> dict[str, object]:
        return {
            "artifact_id": artifact_id,
            "batch_id": batch_id,
            "mall": "rakuten",
            "category": "billpay_document",
            "screen": "settlement_result",
            "document_type": "34",
            "document_kind": "csv",
            "issue_date": issue_date,
            "filename": Path(relative_path).name,
            "relative_path": relative_path,
            "sha256": sha256,
            "row_count": 1,
            "validated": True,
        }

    def _complete_marker(
        self, *, batch_id: str, scope: str, issue_date: str | None = None
    ) -> dict[str, object]:
        # 実運用manifestと同一形状（artifact_id / relative_path / sha256 は None、
        # scope="latest" のとき issue_date は None）。
        return {
            "artifact_id": None,
            "batch_id": batch_id,
            "mall": "rakuten",
            "category": "batch_complete",
            "screen": "settlement_result",
            "scope": scope,
            "document_type": "34",
            "document_kind": "csv",
            "issue_date": issue_date,
            "filename": None,
            "relative_path": None,
            "sha256": None,
            "row_count": 1,
            "validated": True,
        }

    def test_batch_complete_marker_does_not_hide_documents(self) -> None:
        # 2026-07-13 の本番manifestと同一構成（文書1件 + batch_completeマーカー1件）。
        # 修正前はここが 0 件になっていた（str(None)=="None" が最大値として選ばれるため）。
        relative, sha256 = self.write_artifact(
            "billpay_settlement_result_doctype-34_aaa.csv", self._shop_detail_bytes()
        )
        self.write_manifest(
            [
                self._document_record(
                    issue_date="2026-07-03",
                    batch_id="batch1",
                    artifact_id="aaa",
                    relative_path=relative,
                    sha256=sha256,
                ),
                self._complete_marker(batch_id="batch1", scope="latest"),
            ]
        )
        result = bs_rakuten._read_saved_result(
            screen="settlement_result",
            scope="latest",
            issue_date=None,
            document_type="34",
        )
        self.assertEqual(len(result.documents), 1)
        document = result.documents[0]
        self.assertEqual(document.artifact_id, "aaa")
        self.assertEqual(document.document_type, "34")
        self.assertFalse(result.executed)

    def test_none_issue_date_is_never_selected_as_latest(self) -> None:
        # マーカーの issue_date(None) が文字列 "None" として最大値に選ばれないことを、
        # 「マーカーだけ manifest 末尾にある」構成で明示的に固定する。
        relative, sha256 = self.write_artifact(
            "billpay_settlement_result_doctype-34_bbb.csv", self._shop_detail_bytes()
        )
        self.write_manifest(
            [
                self._document_record(
                    issue_date="2026-07-03",
                    batch_id="batch1",
                    artifact_id="bbb",
                    relative_path=relative,
                    sha256=sha256,
                ),
                self._complete_marker(batch_id="batch1", scope="latest"),
            ]
        )
        result = bs_rakuten._read_saved_result(
            screen="settlement_result",
            scope="latest",
            issue_date=None,
            document_type="34",
        )
        self.assertEqual([item.issue_date for item in result.documents], ["2026-07-03"])

    def test_scope_latest_picks_newest_issue_date_only(self) -> None:
        old_relative, old_sha = self.write_artifact(
            "billpay_settlement_result_doctype-34_old.csv",
            self._shop_detail_bytes(issue_date="2026/06/03"),
        )
        new_relative, new_sha = self.write_artifact(
            "billpay_settlement_result_doctype-34_new.csv",
            self._shop_detail_bytes(issue_date="2026/07/03"),
        )
        self.write_manifest(
            [
                self._document_record(
                    issue_date="2026-06-03",
                    batch_id="batch1",
                    artifact_id="old",
                    relative_path=old_relative,
                    sha256=old_sha,
                ),
                self._document_record(
                    issue_date="2026-07-03",
                    batch_id="batch1",
                    artifact_id="new",
                    relative_path=new_relative,
                    sha256=new_sha,
                ),
                self._complete_marker(batch_id="batch1", scope="latest"),
            ]
        )
        result = bs_rakuten._read_saved_result(
            screen="settlement_result",
            scope="latest",
            issue_date=None,
            document_type="34",
        )
        self.assertEqual([item.artifact_id for item in result.documents], ["new"])

    def test_scope_date_returns_requested_issue_date(self) -> None:
        relative, sha256 = self.write_artifact(
            "billpay_settlement_result_doctype-34_ccc.csv",
            self._shop_detail_bytes(issue_date="2026/07/03"),
        )
        self.write_manifest(
            [
                self._document_record(
                    issue_date="2026-07-03",
                    batch_id="batch1",
                    artifact_id="ccc",
                    relative_path=relative,
                    sha256=sha256,
                ),
                self._complete_marker(
                    batch_id="batch1", scope="date", issue_date="2026-07-03"
                ),
            ]
        )
        result = bs_rakuten._read_saved_result(
            screen="settlement_result",
            scope="date",
            issue_date="2026-07-03",
            document_type="34",
        )
        self.assertEqual([item.artifact_id for item in result.documents], ["ccc"])

    def test_no_completed_batch_returns_no_documents(self) -> None:
        # 該当batchが無い（＝一度も取得していない画面）ときは0件で返る。
        self.write_manifest([])
        result = bs_rakuten._read_saved_result(
            screen="billing_check",
            scope="latest",
            issue_date=None,
            document_type="33",
        )
        self.assertEqual(result.documents, ())
        self.assertFalse(result.executed)


class YahooAccessReadSavedResultTest(_ManifestFixture):
    """batch_complete マーカーが「batch_complete/None の保存済みファイルが見つかりません。」
    という実体のない警告を生まないこと（BillPayと同一のバグパターン）。"""

    env_key = "KURIMA_ACCESS_ANALYTICS_DIR"

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            aa_yahoo, "_AUDIT_PATH", self.root / "audit" / "yahoo_access.jsonl"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _product_bytes(self) -> bytes:
        row = [
            "テスト商品", "item001", "", "10000", "1", "1", "1",
            "10%", "0", "0", "5", "0", "5", "0%",
        ]
        return _cp932_crlf_bytes([list(EXPECTED_PRODUCT_HEADER_14), row])

    def _overall_bytes(self) -> bytes:
        header = ["日付", "ページビュー", "セッション合計", "訪問者数"] + [
            f"列{index}" for index in range(5, 25)
        ]
        row = ["2026/07/11", "100", "50", "40"] + ["0"] * 20
        return _cp932_crlf_bytes([header, row])

    def test_batch_complete_marker_does_not_produce_phantom_warning(self) -> None:
        target_label = "2026-07-11..2026-07-11"
        fingerprint = "fp0123456789abcd"
        product_relative, product_sha = self.write_artifact(
            "yahoo_product.csv", self._product_bytes()
        )
        overall_relative, overall_sha = self.write_artifact(
            "yahoo_overall_pc.csv", self._overall_bytes()
        )
        self.write_manifest(
            [
                {
                    "artifact_id": "p1",
                    "batch_id": "batch1",
                    "mall": "yahoo",
                    "category": "product",
                    "device": "unspecified",
                    "account_fingerprint": fingerprint,
                    "target_label": target_label,
                    "filename": "yahoo_product.csv",
                    "relative_path": product_relative,
                    "sha256": product_sha,
                    "row_count": 1,
                },
                {
                    "artifact_id": "o1",
                    "batch_id": "batch1",
                    "mall": "yahoo",
                    "category": "overall",
                    "device": "pc",
                    "account_fingerprint": fingerprint,
                    "target_label": target_label,
                    "filename": "yahoo_overall_pc.csv",
                    "relative_path": overall_relative,
                    "sha256": overall_sha,
                    "row_count": 1,
                },
                {
                    "artifact_id": None,
                    "batch_id": "batch1",
                    "mall": "yahoo",
                    "category": "batch_complete",
                    "device": None,
                    "device_label": None,
                    "account_fingerprint": fingerprint,
                    "target_label": target_label,
                    "filename": None,
                    "relative_path": None,
                    "sha256": None,
                    "row_count": 2,
                },
            ]
        )
        result = aa_yahoo._read_saved_result(
            date(2026, 7, 11),
            date(2026, 7, 11),
            account_fingerprint=fingerprint,
        )
        self.assertIsNotNone(result.product)
        self.assertEqual([item.device for item in result.overall], ["pc"])
        # 修正前はここに "batch_complete/None の保存済みファイルが見つかりません。" が入っていた。
        self.assertEqual(result.warnings, ())
        for warning in result.warnings:
            self.assertNotIn("batch_complete", warning)


class RakutenAccessReadSavedResultTest(_ManifestFixture):
    """楽天アクセス解析は device allowlist で絞るため、マーカーは元から混入しない
    （横展開確認の固定化。将来 device キーの扱いを変えても回帰を検知する）。"""

    env_key = "KURIMA_ACCESS_ANALYTICS_DIR"

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            aa_rakuten, "_AUDIT_PATH", self.root / "audit" / "rakuten_access.jsonl"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _device_csv_bytes(self, device_label: str) -> bytes:
        rows = [
            ["楽天市場 商品ページ分析"],
            ["ショップ: テストショップ"],
            ["対象期間: 2026-07-11"],
            ["表示形式: 日次"],
            [f"対象端末: {device_label}"],
            list(EXPECTED_HEADER_28),
            [
                "1", "101", "", "12345", "テスト商品", "ABC-001", "item001",
                "10000", "1", "1", "5", "5", "20%", "10000", "1", "1", "0", "4",
                "0", "0", "0", "30", "0", "0", "0%", "0", "0", "10",
            ],
        ]
        return _utf8_bom_bytes(rows)

    def test_batch_complete_marker_is_ignored(self) -> None:
        devices = {"pc": "PC", "app": "楽天市場アプリ", "smartphone_web": "スマートフォン"}
        records: list[dict[str, object]] = []
        for device, label in devices.items():
            relative, sha256 = self.write_artifact(
                f"rakuten_item_access_{device}_20260711.csv", self._device_csv_bytes(label)
            )
            records.append(
                {
                    "artifact_id": device,
                    "batch_id": "batch1",
                    "mall": "rakuten",
                    "category": "device_access",
                    "device": device,
                    "device_label": label,
                    "target_label": "2026-07-11",
                    "filename": Path(relative).name,
                    "relative_path": relative,
                    "sha256": sha256,
                    "row_count": 1,
                }
            )
        records.append(
            {
                "artifact_id": None,
                "batch_id": "batch1",
                "mall": "rakuten",
                "category": "batch_complete",
                "device": None,
                "device_label": None,
                "target_label": "2026-07-11",
                "filename": None,
                "relative_path": None,
                "sha256": None,
                "row_count": 3,
            }
        )
        self.write_manifest(records)
        result = aa_rakuten._read_saved_result(date(2026, 7, 11), include_all=False)
        self.assertEqual(
            sorted(item.device for item in result.csv_files),
            ["app", "pc", "smartphone_web"],
        )
        self.assertEqual(result.warnings, ())


class YahooBillingReadSavedResultTest(_ManifestFixture):
    """Yahoo!請求関連は statement_type allowlist で絞るため、マーカー
    （statement_type=None）は元から混入しない（横展開確認の固定化）。"""

    env_key = "KURIMA_BILLING_STATEMENTS_DIR"

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(
            bs_yahoo, "_AUDIT_PATH", self.root / "audit" / "yahoo_statements.jsonl"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _billing_bytes(self) -> bytes:
        rows = [
            list(EXPECTED_BILLING_RECEIPT_HEADER_7),
            ["2026/07/01", "ORDER001", "商品代金", "", "1000", "100", "1100"],
        ]
        return _cp932_crlf_bytes(rows)

    def test_batch_complete_marker_is_ignored(self) -> None:
        fingerprint = "fp0123456789abcd"
        relative, sha256 = self.write_artifact("billing_final_2026-07.csv", self._billing_bytes())
        self.write_manifest(
            [
                {
                    "artifact_id": "b1",
                    "batch_id": "batch1",
                    "mall": "yahoo",
                    "category": "statement",
                    "statement_type": "billing",
                    "state": "final",
                    "closing_date_label": None,
                    "account_fingerprint": fingerprint,
                    "target_label": "2026-07",
                    "filename": Path(relative).name,
                    "relative_path": relative,
                    "sha256": sha256,
                    "row_count": 1,
                },
                {
                    "artifact_id": None,
                    "batch_id": "batch1",
                    "mall": "yahoo",
                    "category": "batch_complete",
                    "statement_type": None,
                    "state": "final",
                    "closing_date_label": None,
                    "account_fingerprint": fingerprint,
                    "target_label": "2026-07",
                    "filename": None,
                    "relative_path": None,
                    "sha256": None,
                    "row_count": 1,
                },
            ]
        )
        result = bs_yahoo._read_saved_result(
            target_month="2026-07",
            requested_types=("billing",),
            account_fingerprint=fingerprint,
        )
        self.assertEqual([item.statement_type for item in result.files], ["billing"])
        self.assertEqual(result.warnings, ())


if __name__ == "__main__":
    unittest.main()
