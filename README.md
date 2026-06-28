# くりまポータルツール

Power Automate Desktop と Excel マクロで実行している処理を、Python のローカル Web アプリへ移行するためのポータルです。

## 現在の実装範囲

- ポータルトップ
- 在庫明細確認
  - Playwright で Next Engine から受注明細一覧 CSV を取得
  - `受注明細一覧/data*.csv` の最新ファイルを自動検出
  - `商品管理シート.xlsm` の `商品マスタ`、`NEオプション一覧`、`しまのや商品マスタ` を参照
  - Power Query と同等の通常商品集計
  - Power Query と同等の選べるセット内訳集計
  - 画面表示と CSV ダウンロード
- ヤマト伝票作成支援
  - Next Engine の受注伝票管理をヤマト発送方法で絞り込み
  - 購入者データCSVを `ネクストエンジン受注データ\購入者データ` に保存
  - 明細一覧の商品情報CSVを `ネクストエンジン受注データ\商品情報データ` に保存
  - 納品書PDFを配送情報出力対象にした状態で一括取得
  - カスタムデータ作成アプリから配送情報CSVを取得
  - `ne-yamatocsv` の最新CSVと `商品管理シート.xlsm` の `品名テーブル_DB` から、B2取込用 `ne-to-yamato*.csv` を作成
  - B2仕様に合わせ、届け先住所と届け先建物名を自動補正
  - `/yamato` でNext Engine一括準備、完成データCSV作成、納品書印刷待ち復旧を実行
  - 納品書PDF取得時の前後ステータス詳細確認は検証時だけ任意で実行
  - Yamato B2本体へのログイン確認、取込画面確認、CSV選択dry-run、明示確認付き実取込

## 起動

```powershell
cd C:\Users\hppym\dev\pad-python\portal_tool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m uvicorn portal_app.main:app --host 127.0.0.1 --port 8000 --reload
```

ブラウザで `http://127.0.0.1:8000` を開きます。

## コマンド確認

```powershell
cd C:\Users\hppym\dev\pad-python\portal_tool
.\.venv\Scripts\python.exe -m portal_app.cli check
```

Next Engine から最新 CSV を取得して再集計します。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli download-next-engine
```

ブラウザを表示してデバッグする場合:

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli download-next-engine --headed --slow-mo-ms 300
```

Next Engine の伝票ステータスを確認します。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli inspect-next-engine-order --order-no 68263 --headed --slow-mo-ms 150
```

納品書印刷済みの伝票を印刷待ちに戻します。複数伝票は1回のログインで順番に処理します。対象が `40:納品書印刷済` または途中状態の `2:起票済(CSV/手入力)` の場合に `20:納品書印刷待ち` へ戻します。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli restore-next-engine-print-wait --order-no 68263 --execute --headed --slow-mo-ms 150
$orders = "68277,68276,68275"
.\.venv\Scripts\python.exe -m portal_app.cli restore-next-engine-print-wait-batch --order-nos $orders --execute --headed --slow-mo-ms 150
.\.venv\Scripts\python.exe -m portal_app.cli restore-next-engine-print-wait-batch --order-nos $orders --headed --slow-mo-ms 100
```

この操作は `logs/next_engine_status/order_status_audit.jsonl` に伝票番号と前後ステータスを記録します。

納品書PDF取得でステータスが動くことを単一伝票で検証し、最後に印刷待ちへ戻します。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli test-next-engine-invoice-download --order-no 68263 --execute --headed --slow-mo-ms 150
```

この操作は `logs/next_engine_status/invoice_download_audit.jsonl` に、納品書取得前、取得後、復旧後のステータスを記録します。

ヤマト発送方法の対象伝票を確認します。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli inspect-next-engine-yamato-orders --headed --slow-mo-ms 150
```

ヤマト向け購入者データCSVを取得します。`--execute` を外すと対象件数確認のみです。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli download-next-engine-yamato-buyer --execute --headed --slow-mo-ms 150
```

ヤマト向け商品情報データCSVを明細一覧から取得します。既定の出力タイプは `D_ALL` です。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli download-next-engine-yamato-product --execute --headed --slow-mo-ms 150
```

購入者/商品情報データCSVの個別取得も、`--order-nos 68280` のように伝票番号を指定して対象を絞れます。

カスタムデータ作成アプリからヤマト配送情報CSVを取得します。`--execute` を付けると Next Engine 側で「配送情報ダウンロード済み」として処理されます。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli download-next-engine-yamato-custom-shipping --headed --slow-mo-ms 150
```

実際に取得する場合:

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli download-next-engine-yamato-custom-shipping --execute --headed --slow-mo-ms 150
```

ヤマトCSV取得結果は `logs/next_engine_yamato/yamato_download_audit.jsonl` に記録します。

ヤマトB2取込CSV用の準備をまとめて実行します。既定ではローカル変換のみです。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2 --write-conversion
```

Next Engine の対象件数確認まで含める場合:

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2 --fetch-next-engine --headed --slow-mo-ms 150
```

購入者データCSVと商品情報データCSVを実際に取得する場合:

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2 --execute-downloads --headed --slow-mo-ms 150
```

納品書PDFまで対象確認する場合:

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2 --check-invoices --headed --slow-mo-ms 150
```

納品書PDFの前後ステータスを1件ずつ詳細確認したい検証時だけ、`--verify-invoice-statuses` を追加します。通常実行では対象一覧の取得だけを行い、受注伝票入力画面は全件分開きません。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2 --check-invoices --verify-invoice-statuses --headed --slow-mo-ms 150
```

購入者/商品CSV取得、納品書PDF取得、配送情報CSV取得、B2取込CSV作成を一括で実行する場合:

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2 --execute-downloads --execute-invoices --execute-custom-shipping --write-conversion --headed --slow-mo-ms 150
```

過去の検証伝票を再処理しないよう、対象伝票番号を指定できます。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli prepare-yamato-b2 --order-nos 68280 --execute-downloads --execute-invoices --execute-custom-shipping --write-conversion --headed --slow-mo-ms 150
```

一括準備結果は `logs/next_engine_yamato/yamato_b2_prepare_audit.jsonl` に記録します。

納品書PDF取得は配送情報CSVの前提です。内部では `mode=H` を選択し、`納品書印刷済` への移動と配送情報出力対象化を同時に行います。配送情報CSVの実ダウンロードは Next Engine 側で「配送情報ダウンロード済み」として処理されます。検証後に戻す場合は `restore-next-engine-print-wait-batch` を使用します。

Yamato B2 Cloud へ取込CSVを渡す境界は、状態変更しない順に実行します。未指定時は `ネクストエンジン\完成データ` の最新 `ne-to-yamato*.csv` を使います。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli import-yamato-b2
.\.venv\Scripts\python.exe -m portal_app.cli import-yamato-b2 --check-login --headed --slow-mo-ms 150
.\.venv\Scripts\python.exe -m portal_app.cli import-yamato-b2 --open-import-page --headed --slow-mo-ms 150
.\.venv\Scripts\python.exe -m portal_app.cli import-yamato-b2 --select-file-dry-run --headed --slow-mo-ms 150
```

実際にB2へ取り込む場合だけ、二重確認として `--execute-import --confirm-import` を両方指定します。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli import-yamato-b2 --execute-import --confirm-import --headed --slow-mo-ms 150
```

B2取込境界の結果は `logs/next_engine_yamato/yamato_b2_import_audit.jsonl` に記録します。画面確認が必要な場合は `logs/next_engine_yamato/b2_import_debug` のHTMLとスクリーンショットを確認します。

Excel の `ne-yamato変換ツール.xlsm` 相当の変換をPythonで実行します。`--write` を外すと確認のみです。

```powershell
.\.venv\Scripts\python.exe -m portal_app.cli convert-yamato-ne-to-b2
.\.venv\Scripts\python.exe -m portal_app.cli convert-yamato-ne-to-b2 --write
```

出力先は `ネクストエンジン\完成データ\ne-to-yamatoYYMMDDHHMM.csv` です。CSV作成時は `logs/next_engine_yamato/yamato_conversion_audit.jsonl` に変換元CSV、出力CSV、行数、住所補正件数、警告を記録します。

住所補正では、B2の住所欄に残っている建物名らしい末尾を建物名欄へ移し、1桁数字だけが建物名欄に分かれている場合は住所末尾へ戻します。過去データ調査から、`様方` / `方`、`A1` / `A-1` 形式、番地・号・数字末尾 + 1桁建物名のパターンも追加しています。文字数超過、環境依存文字、CP932で出力できない文字は要確認として画面に表示します。

Web画面で確認する場合:

```text
http://127.0.0.1:8002/yamato
```

`/yamato` では次の操作ができます。

- 最新 `ne-yamato*.csv` の変換プレビュー
- B2取込CSV作成
- Next Engine一括準備の対象確認
- 購入者/商品CSV取得
- 納品書PDF、配送情報CSV、B2取込CSV作成の一括実行
- 納品書印刷待ちへのバッチ復旧
- Yamato B2取込CSV検証、ログイン確認、取込画面確認、CSV選択dry-run、明示確認付き実取込

## パス検出

既定では次の同期フォルダを自動検出します。

```text
C:\Users\<user>\株式会社しまのや\くりまポータル - ドキュメント
```

別の場所を使う場合は、環境変数 `PORTAL_ROOT` を設定してください。

## Next Engine 認証情報

優先順は次の通りです。

1. `NEXT_ENGINE_LOGIN_ID` と `NEXT_ENGINE_PASSWORD`
2. `NEXT_ENGINE_CREDENTIAL_PATH` の Excel
3. `C:\Users\hppym\開発案件\日別売上集計データダウンロード\docs\ID・PW.xlsx`

Playwright の同梱ブラウザが未インストールの場合は、既存の Chrome/Edge を自動検出します。固定したい場合は `PLAYWRIGHT_CHROMIUM_EXECUTABLE` に `chrome.exe` または `msedge.exe` のパスを設定してください。

## Yamato B2 認証情報

`.env.yamato-b2.example` を参考に、実値は `.env`、`.env.yamato-b2`、または `yamato-b2.env` へ設定します。

```env
YAMATO_B2_LOGIN_ID=...
YAMATO_B2_PASSWORD=...
YAMATO_B2_HEADLESS=false
```
