# 実行ログ・エラーデバッグ設計

## 目的

外部サービス連携を含む処理でエラーが出たときに、画面を見ていた人の記憶に依存せず、ログだけで次を確認できる状態にする。

- どのボタン、どの条件、どの入力ファイルで実行したか
- どのステップまで成功し、どのステップで落ちたか
- その時点のURL、画面タイトル、HTML、スクリーンショット
- Playwrightのconsole/pageerror/dialog/request failed/download履歴
- 作成・取得・アップロード対象ファイル、行数、ハッシュ
- 例外型、メッセージ、Python traceback
- 外部サービス側の状態変化を伴う処理の前後ステータス

## 現状

既にあるもの:

- `logs/clickpost/clickpost_audit.jsonl`
- `logs/clickpost/letterpack_pdf_audit.jsonl`
- `logs/next_engine_yamato/yamato_*_audit.jsonl`
- `logs/next_engine_yamato/b2_import_debug/*.html|*.png`
- `portal_app/services/progress_jobs.py` の画面進捗
- 各サービス内の `warnings`, `skipped_reason`, `screenshot_path`, `html_path`

不足しているもの:

- フロー横断で追える共通の `run_id`
- 進捗ジョブと監査ログの紐付け
- 成功時と失敗時で同じ粒度のステップログ
- ブラウザのconsole/pageerror/requestfailed/dialog/download履歴
- 例外発生時の統一された `exception.json`
- ファイルの存在確認、行数、SHA256などの入力・出力証跡
- UIから「この実行のログを見る」導線

## 基本方針

すべての実行に `run_id` を付ける。

`job_id` はUI進捗用、`run_id` は調査用の永続IDとする。原則として `job_id == run_id` に寄せ、できない場合はジョブ状態に `run_id` を保持する。

既存の監査JSONLは残す。新しい統一ログを追加し、既存ログには `run_id` を入れて横断検索できるようにする。

## 保存構造

```text
logs/runs/
  20260629/
    clickpost_create_uploadcheck_20260629_101500_ab12cd34/
      run.json
      events.jsonl
      artifacts.jsonl
      steps/
        001_create_csv/
          before.json
          after.json
          output_preview.json
        002_upload_check/
          before.json
          after.json
      exceptions/
        exception.json
      browser/
        console.jsonl
        pageerror.jsonl
        requestfailed.jsonl
        dialogs.jsonl
        downloads.jsonl
        final.html
        final.png
        trace.zip        # 必要時のみ
        network.har      # 必要時のみ
```

`run.json` は実行概要、`events.jsonl` は時系列イベント、`artifacts.jsonl` はファイル証跡だけを記録する。

## run.json

```json
{
  "schema_version": 1,
  "run_id": "clickpost_create_uploadcheck_20260629_101500_ab12cd34",
  "flow": "clickpost",
  "action": "create_csv_upload_check",
  "entrypoint": "web",
  "status": "failed",
  "started_at": "2026-06-29T10:15:00+09:00",
  "finished_at": "2026-06-29T10:15:41+09:00",
  "elapsed_ms": 41000,
  "operator_context": {
    "route": "/clickpost/create-csv-upload-check/start",
    "headed": false,
    "slow_mo_ms": 0
  },
  "result_summary": {
    "target_rows": 9,
    "output_csv": "${PORTAL_ROOT}/CP・LPP宛名作成ツール/完成したデータ/clickpostimport.csv"
  },
  "error": {
    "type": "RuntimeError",
    "message": "CSVヘッダーが不足しています。",
    "exception_path": "exceptions/exception.json"
  }
}
```

## events.jsonl

1行1イベント。処理中の進捗画面もこのイベントを元に更新できるようにする。

```json
{
  "ts": "2026-06-29T10:15:12+09:00",
  "run_id": "clickpost_create_uploadcheck_20260629_101500_ab12cd34",
  "level": "info",
  "flow": "clickpost",
  "step_key": "create_csv",
  "event": "step.completed",
  "message": "clickpostimport.csv を作成しました。",
  "elapsed_ms": 8200,
  "payload": {
    "rows": 9,
    "output_csv": "${PORTAL_ROOT}/.../clickpostimport.csv"
  }
}
```

必須イベント:

- `run.started`
- `step.started`
- `step.completed`
- `step.failed`
- `artifact.created`
- `browser.console`
- `browser.pageerror`
- `browser.requestfailed`
- `browser.dialog`
- `download.completed`
- `run.completed`
- `run.failed`

## exception.json

例外は必ず構造化して残す。

```json
{
  "type": "PlaywrightTimeoutError",
  "message": "locator.click: Timeout 30000ms exceeded",
  "traceback": "...",
  "step_key": "b2_import",
  "last_page": {
    "url": "https://newb2web.kuronekoyamato.co.jp/ex_data_import.html",
    "title": "外部データから発行｜B2クラウド",
    "screenshot": "browser/failure.png",
    "html": "browser/failure.html"
  },
  "context": {
    "selector": "#import_start",
    "csv_file": "${PORTAL_ROOT}/.../ne-to-yamato2606282326.csv"
  }
}
```

## ブラウザ証跡

Playwrightを使う処理は、ページ作成直後に必ずイベントを登録する。

- `page.on("console")`
- `page.on("pageerror")`
- `page.on("requestfailed")`
- `page.on("dialog")`
- `page.on("download")`
- `context.tracing.start()` は `PORTAL_DEBUG_TRACE=1` の時だけ有効
- HARは `PORTAL_DEBUG_HAR=1` の時だけ有効

必ず保存するもの:

- 失敗時の `failure.png`
- 失敗時の `failure.html`
- 失敗時の `page.url`
- 失敗時の `page.title`
- 直近のconsole/pageerror/requestfailed

成功時は容量を抑えるため、重要境界だけ保存する。

- ログイン後
- CSV選択後
- 取込結果画面
- PDF/CSVダウンロード完了後

## ファイル証跡

入力・出力ファイルは本文を丸ごとログに入れない。代わりにメタデータを記録する。

```json
{
  "kind": "input_csv",
  "path": "${PORTAL_ROOT}/.../buyer.csv",
  "exists": true,
  "size": 12345,
  "sha256": "abc123...",
  "rows": 42,
  "headers": ["伝票番号", "購入者名"],
  "created_at": "2026-06-29T10:10:00+09:00",
  "modified_at": "2026-06-29T10:12:00+09:00"
}
```

CSVプレビューは最大10行まで。個人情報を含む場合は、UI表示用と調査用を分ける。

## 秘密情報・個人情報

ログに出してはいけないもの:

- パスワード
- セッションCookie
- storage state JSON本体
- APIキー
- クレジットカード、Yahoo決済情報

マスク対象:

- 電話番号: 末尾4桁以外をマスク
- メールアドレス: ローカル部を一部マスク
- 住所: UI表示では町名以降を省略、ローカルHTML証跡には残す

HTML保存前には既存の `_mask_debug_sensitive_fields` 相当を全ブラウザ処理に適用する。

## 文字化け防止

ログとUIメッセージは必ずUTF-8で扱う。

- PythonファイルはUTF-8で保存する
- JSONLは `encoding="utf-8", newline="\n"` で書く
- `json.dumps(..., ensure_ascii=False)` を使う
- CLI出力は `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` を入口で行う
- テストで代表メッセージに `�` や `繧`, `縺`, `譁` が混ざっていないことを確認する

現在のコードには一部、過去の文字化けメッセージが残っている。実装時は共通ロガー導入前に、ユーザー向けメッセージと進捗メッセージをUTF-8日本語へ戻す。

## UI設計

進捗パネルに次を出す。

- 実行ID
- 現在のステップ
- ステータス
- 開始時刻、経過時間
- 完了時の出力ファイル
- エラー時の要約
- `ログフォルダを開く` ボタン
- `デバッグ情報を開く` ボタン

通常ユーザーには短い説明だけ表示し、詳細ログは折りたたむ。

```text
処理状況
実行ID: clickpost_create_uploadcheck_20260629_101500_ab12cd34

[完了] CSV作成 9件
[エラー] アップロード前チェック セレクタ #file が見つかりません

ログ: logs/runs/20260629/clickpost_create_uploadcheck_...
```

## 実装対象

### 1. 共通ロガー

新規: `portal_app/services/execution_logging.py`

責務:

- `run_id` 採番
- `run.json` 作成・更新
- `events.jsonl` 追記
- `artifacts.jsonl` 追記
- 例外JSON保存
- ファイルメタデータ保存
- Playwrightページ証跡保存

API案:

```python
with start_run(flow="clickpost", action="create_csv_upload_check", entrypoint="web") as run:
    with run.step("create_csv", "CSV作成") as step:
        result = create_clickpost_csv(...)
        step.artifact("output_csv", result.output_csv)
```

Playwright用:

```python
await run.attach_page(page, step_key="b2_import")
await run.capture_page(page, label="after_file_select")
```

### 2. progress_jobs連携

`ProgressJob` に `run_id`, `log_dir`, `events_tail` を追加する。

`progress_jobs.update_step()` は画面進捗だけでなく `events.jsonl` にも同じイベントを出す。

### 3. クリックポスト適用

対象:

- CSV作成＋アップロード前チェック
- NE取得＋CSV作成
- 取込・決済・送り状番号取得
- レターパックPDF作成

特に本番処理の `取込・決済・送り状番号取得` は、各外部状態変化の直前直後を必ず残す。

### 4. ヤマト適用

対象:

- NE側取得
- B2 CSV変換
- B2ログイン
- B2取込
- 納品書印刷待ち復旧

B2取込では既存の `b2_import_debug` を `run_id` 配下へ移すか、少なくとも `artifacts.jsonl` にパスを記録する。

### 5. ログビュー

追加ルート:

- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/events`
- `GET /runs/{run_id}/artifact?path=...`

最初はクリックポスト画面・ヤマト画面の進捗パネルから該当runのパスを表示するだけでもよい。

## エラー分類

`error_category` を必ず付ける。

- `input_file`: CSVなし、ヘッダー不足、行数0
- `conversion`: 住所分割、マスタ未一致、文字数超過
- `auth`: ログイン失敗、認証切れ
- `selector`: ボタンや入力欄が見つからない
- `external_validation`: B2やクリックポスト側の入力エラー
- `download`: ダウンロード未検出、保存失敗
- `upload`: アップロード失敗、結果画面未確認
- `payment`: 決済ボタン不足、決済後画面未確認
- `system`: 予期しない例外

## 受け入れ条件

失敗した実行について、次が必ず満たされること。

- UIに `run_id` が表示される
- `logs/runs/YYYYMMDD/<run_id>/run.json` が存在する
- `events.jsonl` に `run.started`, `step.started`, `step.failed`, `run.failed` がある
- `exceptions/exception.json` がある
- ブラウザ処理なら `failure.png` と `failure.html` がある
- 入力CSVと出力CSVのパス、行数、SHA256がある
- Python tracebackがある
- パスワード、Cookie、storage state本体が含まれない

成功した実行について、次が必ず満たされること。

- `run.json.status == "completed"`
- 主要ステップがすべて `completed`
- 出力ファイルのパス、行数、SHA256がある
- 既存の監査ログにも `run_id` が入っている

## 導入順

1. `execution_logging.py` を追加し、単体テストを作る
2. `progress_jobs.py` に `run_id` と永続イベント追記を追加
3. クリックポストの `CSV作成＋アップロード前チェック` に適用
4. クリックポストの本番処理に適用
5. Yamato B2取込に適用
6. Next Engineダウンロード系に適用
7. `/runs` のログ閲覧画面を追加
8. 古いログのローテーションを追加

## ローテーション

初期値:

- `logs/runs`: 60日保持
- `trace.zip`, `network.har`: 14日保持
- HTML/PNG: 30日保持
- audit JSONL: 180日保持

削除前に `run.json` と `events.jsonl` だけは残す選択も可能にする。

## 既存コードへの影響

処理本体の戻り値や外部操作は変えない。

追加するのは次の横断機能だけ。

- 実行開始時の `run_id`
- ステップ開始・完了・失敗イベント
- ファイルメタデータ
- ブラウザ証跡
- 例外保存
- UIのログ導線

そのため、まずクリックポストの安全なdry-run系から適用し、本番系へ広げる。
