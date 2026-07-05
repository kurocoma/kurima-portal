# くりまポータルツール

Power Automate Desktop と Excel マクロで実行していた業務（在庫明細確認・ヤマト伝票・クリックポスト）を、
Python のローカル Web アプリへ移行するためのポータルです。
SharePoint「くりまポータル」ライブラリの同期フォルダを読み書きし、
Playwright で Next Engine / ヤマトB2 / クリックポストを自動操作します。

## 機能

- **ポータルトップ** — 業務の入口。保存先パスと受注明細データの鮮度（最新CSVのファイル名・取得日時）を表示
- **在庫明細確認** — Next Engine から受注明細CSVを取得し、商品マスタと照合して発注用の集計を作成（通常/高江洲タブ、CSV・PDF出力）
- **ヤマト伝票** — 受注データ取得、住所補正、B2取込用CSV作成、ヤマトB2への取込
- **クリックポスト** — 申込データ作成、レターパック宛名PDF、取込・決済・送り状番号取得
- **実行履歴**（`/jobs`）— バックグラウンドジョブの完了/失敗履歴。`logs/jobs/history.jsonl` に永続化され、再起動後も残る
- **ログ一覧**（`/logs`）— `logs/` 配下のエラー・デバッグ出力を画面から一覧・閲覧

## 前提

- Windows 10/11（Playwright の実ブラウザ操作と SharePoint 同期フォルダを使うため）
- [uv](https://docs.astral.sh/uv/)（Python 本体と依存関係の管理。Python 3.11 以上は uv が自動解決）
- SharePoint「くりまポータル」ライブラリが OneDrive で同期済みであること
  （既定の検出先: `C:\Users\<ユーザー名>\株式会社しまのや\くりまポータル - ドキュメント`）

uv が未導入の場合:

```powershell
winget install astral-sh.uv
# または
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## セットアップ（新しいPC）

```powershell
git clone https://github.com/kurocoma/kurima-portal.git
cd kurima-portal

# 1. 依存パッケージのインストール（uv.lock どおりに再現される）
uv sync

# 2. Playwright ブラウザ（chromium）のインストール
uv run playwright install chromium

# 3. 環境変数ファイルの作成（実値を記入。コミットされない）
copy .env.example .env
notepad .env

# 4. 環境診断（必要なツール・パス・環境変数が揃っているか ○× で検査）
uv run python scripts/doctor.py
```

`doctor.py` は Python バージョン / uv / Playwright ブラウザ実体 / ポータル同期フォルダの解決 /
認証系環境変数の設定状況を検査し、不足があればインストールコマンドを表示します（終了コード = 失敗数）。

## 起動

### ローカルのみ（自分のPCだけで使う）

```powershell
.\scripts\serve.ps1
# または直接:
uv run uvicorn portal_app.main:app --host 127.0.0.1 --port 8006
```

ブラウザで `http://127.0.0.1:8006/` を開きます。ポートは `-Port 8010` か環境変数 `KURIMA_PORT` で変更できます。

### LAN 共有（他のPCから使う）

ホストPC 1台でアプリを起動し、他のPCはブラウザだけで利用します。

```powershell
.\scripts\serve.ps1 -Mode lan
```

初回のみ、ホストPCでファイアウォールの受信許可が必要です（管理者 PowerShell で実行）:

```powershell
New-NetFirewallRule -DisplayName "kurima-portal" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8006 -Profile Private,Domain
```

他のPCからは `http://<ホストPCの名前またはIP>:8006/` を開きます（IPは `ipconfig` の IPv4 アドレス）。

> **重要（安全上の注意）**: このツールにはクリックポストの**実決済**・ヤマトB2の**実取込**を実行するボタンが含まれます。
> 必ず**社内LAN限定**で使い、公開インターネットやクラウド（Vercel等のホスティング）には**絶対に公開しないでください**。
> ルーターのポート開放・トンネルサービスの使用も禁止です。

## 環境変数

`.env.example` を `.env` にコピーして設定します（全キーの説明は `.env.example` 参照）。

| キー | 用途 |
|---|---|
| `KURIMA_PORTAL_ROOT` | ポータル同期フォルダ。未設定なら既定候補を自動探索（`PORTAL_ROOT` も後方互換で有効） |
| `KURIMA_MASTER_BOOK` / `KURIMA_ORDER_CSV_DIR` / `KURIMA_TOOL_DIR` | 商品管理シート / 受注明細フォルダ / ツールフォルダの個別上書き（通常は不要） |
| `KURIMA_PORT` | serve.ps1 の既定ポート（既定 8006） |
| `NEXT_ENGINE_LOGIN_ID` / `NEXT_ENGINE_PASSWORD` | Next Engine ログイン（または `NEXT_ENGINE_CREDENTIAL_PATH` で認証Excel指定） |
| `YAMATO_B2_LOGIN_ID` / `YAMATO_B2_PASSWORD` | ヤマトB2ログイン |
| `CLICKPOST_YAHOO_LOGIN_ID` / `CLICKPOST_YAHOO_PASSWORD` | クリックポスト（Yahoo! ID）ログイン |
| `PLAYWRIGHT_CHROMIUM_EXECUTABLE` | 使用ブラウザを固定したい場合の chrome.exe / msedge.exe パス |

パス系キーが未設定の場合は、`%USERPROFILE%\株式会社しまのや\くりまポータル - ドキュメント` 等の
既定候補を自動探索します（現行PCは無設定で動作します）。

## 画面の説明

| URL | 内容 |
|---|---|
| `/` | ポータルトップ。保存先パスの確認と受注明細データの鮮度表示 |
| `/inventory` | 在庫明細確認（通常/高江洲タブ）。プレビューは非同期読込・キャッシュ付き |
| `/yamato` | ヤマト伝票。NE取得〜B2取込CSV作成〜B2取込 |
| `/clickpost` | クリックポスト。CSV作成〜取込・決済・送り状取得 |
| `/jobs` | 実行履歴。日時・種別・結果・所要時間・エラー概要を新しい順に表示 |
| `/logs` | ログ一覧。`logs/` 配下のファイルを新しい順に一覧し、テキストログを画面で閲覧 |
| `/health` | 死活監視用（`{"status": "ok"}`） |

## トラブルシュート

- **「保存先を自動検出できませんでした」** — SharePoint ライブラリの同期を確認するか、`.env` に `KURIMA_PORTAL_ROOT` を設定。`uv run python scripts/doctor.py` で候補パスを確認できます。
- **ブラウザが起動しない / NotImplementedError** — `uv run playwright install chromium` を実行。Chrome/Edge がインストール済みなら自動検出されます。
- **ジョブが失敗した** — `/jobs` の該当行から「詳細ログ」を開くか、`/logs` で `execution_runs/` 配下の events.jsonl を確認。
- **.py を変更したのに反映されない** — uvicorn は `--reload` なしだと自動反映されません。`.\scripts\restart.bat` で再起動してください（既存プロセスの停止→新しいウィンドウで起動まで自動。`restart.bat 8010` でポート指定、`restart.bat 8006 lan` で LAN 公開モード）。
- **他のPCから繋がらない** — ホストPCのファイアウォール受信許可（上記）と、`-Mode lan` で起動しているか（`0.0.0.0` で LISTEN しているか）を確認。

## CLI リファレンス

Web 画面と同じ処理をコマンドラインから個別に実行できます（検証・デバッグ用）。
`uv run python -m portal_app.cli <コマンド>` の形式で実行します。

```powershell
uv run python -m portal_app.cli check
```

Next Engine から最新 CSV を取得して再集計します。

```powershell
uv run python -m portal_app.cli download-next-engine
```

ブラウザを表示してデバッグする場合:

```powershell
uv run python -m portal_app.cli download-next-engine --headed --slow-mo-ms 300
```

Next Engine の伝票ステータスを確認します。

```powershell
uv run python -m portal_app.cli inspect-next-engine-order --order-no 68263 --headed --slow-mo-ms 150
```

納品書印刷済みの伝票を印刷待ちに戻します。複数伝票は1回のログインで順番に処理します。対象が `40:納品書印刷済` または途中状態の `2:起票済(CSV/手入力)` の場合に `20:納品書印刷待ち` へ戻します。

```powershell
uv run python -m portal_app.cli restore-next-engine-print-wait --order-no 68263 --execute --headed --slow-mo-ms 150
$orders = "68277,68276,68275"
uv run python -m portal_app.cli restore-next-engine-print-wait-batch --order-nos $orders --execute --headed --slow-mo-ms 150
uv run python -m portal_app.cli restore-next-engine-print-wait-batch --order-nos $orders --headed --slow-mo-ms 100
```

この操作は `logs/next_engine_status/order_status_audit.jsonl` に伝票番号と前後ステータスを記録します。

納品書PDF取得でステータスが動くことを単一伝票で検証し、最後に印刷待ちへ戻します。

```powershell
uv run python -m portal_app.cli test-next-engine-invoice-download --order-no 68263 --execute --headed --slow-mo-ms 150
```

この操作は `logs/next_engine_status/invoice_download_audit.jsonl` に、納品書取得前、取得後、復旧後のステータスを記録します。

ヤマト発送方法の対象伝票を確認します。

```powershell
uv run python -m portal_app.cli inspect-next-engine-yamato-orders --headed --slow-mo-ms 150
```

ヤマト向け購入者データCSVを取得します。`--execute` を外すと対象件数確認のみです。

```powershell
uv run python -m portal_app.cli download-next-engine-yamato-buyer --execute --headed --slow-mo-ms 150
```

ヤマト向け商品情報データCSVを明細一覧から取得します。既定の出力タイプは `D_ALL` です。

```powershell
uv run python -m portal_app.cli download-next-engine-yamato-product --execute --headed --slow-mo-ms 150
```

購入者/商品情報データCSVの個別取得も、`--order-nos 68280` のように伝票番号を指定して対象を絞れます。

カスタムデータ作成アプリからヤマト配送情報CSVを取得します。`--execute` を付けると Next Engine 側で「配送情報ダウンロード済み」として処理されます。

```powershell
uv run python -m portal_app.cli download-next-engine-yamato-custom-shipping --headed --slow-mo-ms 150
uv run python -m portal_app.cli download-next-engine-yamato-custom-shipping --execute --headed --slow-mo-ms 150
```

ヤマトCSV取得結果は `logs/next_engine_yamato/yamato_download_audit.jsonl` に記録します。

ヤマトB2取込CSV用の準備をまとめて実行します。既定ではローカル変換のみです。

```powershell
uv run python -m portal_app.cli prepare-yamato-b2
uv run python -m portal_app.cli prepare-yamato-b2 --write-conversion
```

Next Engine の対象件数確認まで含める場合:

```powershell
uv run python -m portal_app.cli prepare-yamato-b2 --fetch-next-engine --headed --slow-mo-ms 150
```

購入者データCSVと商品情報データCSVを実際に取得する場合:

```powershell
uv run python -m portal_app.cli prepare-yamato-b2 --execute-downloads --headed --slow-mo-ms 150
```

納品書PDFまで対象確認する場合:

```powershell
uv run python -m portal_app.cli prepare-yamato-b2 --check-invoices --headed --slow-mo-ms 150
```

納品書PDFの前後ステータスを1件ずつ詳細確認したい検証時だけ、`--verify-invoice-statuses` を追加します。通常実行では対象一覧の取得だけを行い、受注伝票入力画面は全件分開きません。

```powershell
uv run python -m portal_app.cli prepare-yamato-b2 --check-invoices --verify-invoice-statuses --headed --slow-mo-ms 150
```

購入者/商品CSV取得、納品書PDF取得、配送情報CSV取得、B2取込CSV作成を一括で実行する場合:

```powershell
uv run python -m portal_app.cli prepare-yamato-b2 --execute-downloads --execute-invoices --execute-custom-shipping --write-conversion --headed --slow-mo-ms 150
```

過去の検証伝票を再処理しないよう、対象伝票番号を指定できます。

```powershell
uv run python -m portal_app.cli prepare-yamato-b2 --order-nos 68280 --execute-downloads --execute-invoices --execute-custom-shipping --write-conversion --headed --slow-mo-ms 150
```

一括準備結果は `logs/next_engine_yamato/yamato_b2_prepare_audit.jsonl` に記録します。

納品書PDF取得は配送情報CSVの前提です。内部では `mode=H` を選択し、`納品書印刷済` への移動と配送情報出力対象化を同時に行います。配送情報CSVの実ダウンロードは Next Engine 側で「配送情報ダウンロード済み」として処理されます。検証後に戻す場合は `restore-next-engine-print-wait-batch` を使用します。

Yamato B2 Cloud へ取込CSVを渡す境界は、状態変更しない順に実行します。未指定時は `ネクストエンジン\完成データ` の最新 `ne-to-yamato*.csv` を使います。

```powershell
uv run python -m portal_app.cli import-yamato-b2
uv run python -m portal_app.cli import-yamato-b2 --check-login --headed --slow-mo-ms 150
uv run python -m portal_app.cli import-yamato-b2 --open-import-page --headed --slow-mo-ms 150
uv run python -m portal_app.cli import-yamato-b2 --select-file-dry-run --headed --slow-mo-ms 150
```

実際にB2へ取り込む場合だけ、二重確認として `--execute-import --confirm-import` を両方指定します。

```powershell
uv run python -m portal_app.cli import-yamato-b2 --execute-import --confirm-import --headed --slow-mo-ms 150
```

B2取込境界の結果は `logs/next_engine_yamato/yamato_b2_import_audit.jsonl` に記録します。画面確認が必要な場合は `logs/next_engine_yamato/b2_import_debug` のHTMLとスクリーンショット（`/logs` からも閲覧可能）を確認します。

Excel の `ne-yamato変換ツール.xlsm` 相当の変換をPythonで実行します。`--write` を外すと確認のみです。

```powershell
uv run python -m portal_app.cli convert-yamato-ne-to-b2
uv run python -m portal_app.cli convert-yamato-ne-to-b2 --write
```

出力先は `ネクストエンジン\完成データ\ne-to-yamatoYYMMDDHHMM.csv` です。CSV作成時は `logs/next_engine_yamato/yamato_conversion_audit.jsonl` に変換元CSV、出力CSV、行数、住所補正件数、警告を記録します。

住所補正では、B2の住所欄に残っている建物名らしい末尾を建物名欄へ移し、1桁数字だけが建物名欄に分かれている場合は住所末尾へ戻します。過去データ調査から、`様方` / `方`、`A1` / `A-1` 形式、番地・号・数字末尾 + 1桁建物名のパターンも追加しています。文字数超過、環境依存文字、CP932で出力できない文字は要確認として画面に表示します。

## 認証情報の解決順

### Next Engine

1. `NEXT_ENGINE_LOGIN_ID` と `NEXT_ENGINE_PASSWORD`
2. `NEXT_ENGINE_CREDENTIAL_PATH` で指定した Excel（A列=サイト名, B列=ID, C列=PW）

Playwright の同梱ブラウザが未インストールの場合は、既存の Chrome/Edge を自動検出します。固定したい場合は `PLAYWRIGHT_CHROMIUM_EXECUTABLE` に `chrome.exe` または `msedge.exe` のパスを設定してください。

### Yamato B2

`.env.example` を参考に、実値は `.env`、`.env.yamato-b2`、または `yamato-b2.env` へ設定します。

```env
YAMATO_B2_LOGIN_ID=...
YAMATO_B2_PASSWORD=...
YAMATO_B2_HEADLESS=false
```
