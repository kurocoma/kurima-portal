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
- **ログ一覧**（`/logs`）— `logs/` 配下のエラー・デバッグ出力に加え、共有ログ（SharePoint 側の実行ログ・エラーログ）も画面から一覧・閲覧
- **設定**（`/settings`）— .env の設定状況（秘密はマスク）とパス解決・ログ出力先・タイムアウト実効値の見える化（閲覧のみ）
- **進捗表示の共通機能** — ジョブ実行中はナビ「実行履歴」に件数バッジが出て、ページを
  リロード・再訪しても進捗パネルへ自動で再接続。完了/失敗時はタブタイトル・通知音
  （デスクトップ通知は localhost のみ）でお知らせ

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

**かんたんな方法（推奨）**: リポジトリ直下の `setup.bat` を実行（ダブルクリック）します。
uv の導入（未導入なら winget → 公式スクリプトの順で自動導入）→ `uv sync` →
Playwright ブラウザ導入 → `.env` 作成 → 環境診断（`scripts/doctor.py`）まで自動で行います。
リポジトリ未取得の PC では `setup.bat` 1 ファイルだけコピーして実行すれば、
`%USERPROFILE%\kurima-portal` への `git clone` から自動で行います（Git for Windows が必要）。

手動で行う場合:

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

## 更新（導入済みのPC）

リポジトリ直下の `update.bat` を実行（ダブルクリック）します。
`git pull` → `uv sync` → Playwright ブラウザ確認まで自動で行い、最後に
「サーバーを再起動しますか」の確認が出ます（Y でそのまま `scripts\restart.bat` を実行。
30 秒無応答なら再起動せず終了します）。

確認なしで再起動まで一括実行する場合:

```bat
update.bat /restart
update.bat /restart 8006   … ポート指定つき
```

再起動しないと旧コードのまま動き続けます（画面フッターの「稼働版」と `/health` の
`restart_required` で再起動忘れを確認できます）。

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

利用する端末を絞る場合は、ホストPCの `.env` に `KURIMA_ALLOWED_CLIENTS` を設定します
（環境変数の表を参照。未設定なら従来どおり無制限）。許可されていない端末からのアクセスは
403 と日本語の案内文になります。

### ホストPCの自動起動・自動復旧（常駐化）

Windows Update 等でホストPCが再起動しても手動起動が不要になるよう、
タスクスケジューラへの登録スクリプトを用意しています（ホストPCで実行）:

```bat
scripts\register_autostart.bat            … ログオン時に serve.ps1 -Mode lan を自動起動
scripts\register_autostart.bat /watchdog  … 上記＋5分毎の死活監視（/health 無応答なら restart.bat で自動復旧）
scripts\register_autostart.bat /status    … 登録状態の確認
scripts\register_autostart.bat /delete    … すべて解除
```

死活監視の実行結果は `logs\watchdog.log` に記録されます（正常時は記録しません）。
「アクセスが拒否されました」と出る環境では、コマンドプロンプトを管理者として実行してください。

## 環境変数

`.env.example` を `.env` にコピーして設定します。実装が参照する設定キーの一覧は以下のとおりです
（現行PCは無設定で動作します。OS 標準の環境変数 `COMPUTERNAME` / `USERPROFILE` /
`LOCALAPPDATA` / `OneDrive` / `OneDriveCommercial` も参照しますが、設定は不要です）。

### パス・基本設定

| キー | 用途 |
|---|---|
| `KURIMA_PORTAL_ROOT` | ポータル同期フォルダ。未設定なら既定候補を自動探索（`PORTAL_ROOT` も後方互換で有効） |
| `KURIMA_MASTER_BOOK` / `KURIMA_ORDER_CSV_DIR` / `KURIMA_TOOL_DIR` | 商品管理シート / 受注明細フォルダ / ツールフォルダの個別上書き（通常は不要） |
| `KURIMA_PORT` | serve.ps1 / restart.bat の既定ポート（既定 8006） |
| `KURIMA_ALLOWED_CLIENTS` | LAN公開時に利用を許可する接続元（カンマ区切りの IP / CIDR / 前方一致プレフィックス。例 `192.168.1.10, 192.168.20.0/24, 10.0.`）。**未設定なら無制限（従来どおり）**。ホストPC自身（127.0.0.1）は常に許可。許可外は 403 |

### ブラウザ操作タイムアウト

回線やサイトが遅い日に、コードを変更せず `.env` の変更＋再起動だけで時間切れを回避するための設定です。

| キー | 用途 |
|---|---|
| `KURIMA_NAV_TIMEOUT_MS` | 画面遷移（ログイン・ページ移動・再読込）の待ち時間ms。未設定なら現行どおり 60000 |
| `KURIMA_DOWNLOAD_TIMEOUT_MS` | CSV / PDF ダウンロードの待ち時間ms。未設定なら現行どおりサイト別の既定（60000〜180000） |

### ログ関連

| キー | 用途 |
|---|---|
| `KURIMA_LOG_DIR` | 実行ログ・エラーログの出力先を上書き。未設定なら SharePoint 同期フォルダの `神里\くりまポータルエラーログ`（同期フォルダが無いPCは `logs/` に fallback） |
| `KURIMA_LOG_MAX_MB` | 実行ログ・エラーログのローテーション上限サイズMB（既定 5。0 でローテーション無効） |
| `KURIMA_LOG_BACKUP_COUNT` | ローテーションの保持世代数（既定 3、最小 1。`portal-run-<PC名>.log.1` の形で保持） |
| `KURIMA_LOG_SUFFIX` | 共有ログのファイル名サフィックスを明示上書き（既定はコンピュータ名。PC別分離で同期競合を回避） |
| `KURIMA_LOG_RETENTION_DAYS` | `logs/` 配下（ジョブ詳細ログ・B2デバッグ出力）の保持日数。起動時に古いものを自動削除（既定 30。0 以下で無効） |
| `KURIMA_JOB_HISTORY_MAX_LINES` | 実行履歴 `logs/jobs/history.jsonl` の保持行数上限（既定 2000。0 以下で無効） |

### Next Engine

| キー | 用途 |
|---|---|
| `NEXT_ENGINE_LOGIN_ID` / `NEXT_ENGINE_PASSWORD` | Next Engine ログイン |
| `NEXT_ENGINE_CREDENTIAL_PATH` | 認証情報Excel（A列=サイト名, B列=ID, C列=PW）のパス。ID/PW 未設定時に使用 |
| `NEXT_ENGINE_HEADLESS` | NE操作ブラウザの非表示実行（既定 true。false で画面表示） |

### ヤマトB2

| キー | 用途 |
|---|---|
| `YAMATO_B2_LOGIN_ID` / `YAMATO_B2_PASSWORD` | ヤマトB2ログイン |
| `YAMATO_B2_CLASS_CODE` / `YAMATO_B2_PERSONAL_ID` | 法人ログインの分類コード / 個人ID（使用時のみ） |
| `YAMATO_B2_URL` | B2ログインURLの上書き（通常は不要） |
| `YAMATO_B2_STORAGE_STATE` | ログインセッション保存先（storage_state JSON）の上書き |
| `YAMATO_B2_HEADLESS` | B2操作ブラウザの非表示実行（未設定時は `NEXT_ENGINE_HEADLESS` に追従） |
| `KURIMA_B2_CHROME_PATH` | B2取込に使う実ブラウザ（chrome.exe / msedge.exe）のパス上書き |
| `KURIMA_B2_CHROME_PROFILE` | B2専用ブラウザプロファイルの保存先（既定 `data/b2_chrome_profile`） |
| `KURIMA_B2_CHROME_PORT` | B2実ブラウザの CDP デバッグポート（既定 9333） |
| `KURIMA_B2_OPEN_URL` | B2ブラウザ起動時に開くURLの上書き |

### クリックポスト

| キー | 用途 |
|---|---|
| `CLICKPOST_YAHOO_LOGIN_ID` / `CLICKPOST_YAHOO_PASSWORD` | クリックポスト（Yahoo! ID）ログイン |
| `CLICKPOST_SECURITYCODE` | Yahoo! ログインのセキュリティコード（要求される環境のみ） |
| `CLICKPOST_HEADLESS` | クリックポスト操作ブラウザの非表示実行（既定 true） |

### 出荷確定（取込対象の遡り日数）

| キー | 用途 |
|---|---|
| `KURIMA_SHIPMENT_BUYER_LOOKBACK_DAYS` | 購入者データの遡り日数（既定 20） |
| `KURIMA_SHIPMENT_CLICKPOST_LOOKBACK_DAYS` | クリックポスト送り状の遡り日数（既定 20） |
| `KURIMA_SHIPMENT_LETTERPACK_LOOKBACK_DAYS` | レターパック伝票の遡り日数（既定 30） |
| `KURIMA_SHIPMENT_YAMATO_LOOKBACK_DAYS` | ヤマト発行済データの遡り日数（既定 30） |

### ブラウザ実体（Playwright）

| キー | 用途 |
|---|---|
| `PLAYWRIGHT_CHROMIUM_EXECUTABLE` | 使用ブラウザを固定したい場合の chrome.exe / msedge.exe パス |
| `PLAYWRIGHT_BROWSERS_PATH` | Playwright 同梱ブラウザの導入先（Playwright 標準。通常は不要、doctor の診断対象） |

パス系キーが未設定の場合は、`%USERPROFILE%\株式会社しまのや\くりまポータル - ドキュメント` 等の
既定候補を自動探索します（現行PCは無設定で動作します）。

## 画面の説明

| URL | 内容 |
|---|---|
| `/` | ポータルトップ。保存先パスの確認と受注明細データの鮮度表示 |
| `/inventory` | 在庫明細確認（通常/高江洲タブ）。プレビューは非同期読込・キャッシュ付き |
| `/yamato` | ヤマト伝票。NE取得〜B2取込CSV作成〜B2取込 |
| `/clickpost` | クリックポスト。CSV作成〜取込・決済・送り状取得 |
| `/jobs` | 実行履歴。日時・種別・結果・所要時間・エラー概要を新しい順に表示。実行中の件数はナビのバッジに常時表示 |
| `/logs` | ログ一覧。`logs/` 配下と共有ログ（SharePoint 側、「共有」ラベル）を新しい順に一覧し、テキストログを画面で閲覧 |
| `/settings` | 設定の見える化（閲覧のみ）。環境変数の設定状況（PASSWORD 等は先頭2文字＋***でマスク）、パス解決結果、ログ出力先、タイムアウト実効値、稼働バージョン |
| `/health` | 死活監視＋稼働バージョン確認。`status` / `version`（稼働中コードの commit） / `started_at`（起動時刻） / `head_on_disk`（ディスク上の commit） / `restart_required`（true なら更新済み・再起動忘れ） |

## ログ出力（実行ログ・エラーログ）

アプリの実行ログとエラーログは SharePoint 同期フォルダへ出力され、
同期がつながっているすべての PC から同じ場所で参照できます。
複数PCの同時書き込みによる OneDrive の同期競合を避けるため、
ファイル名には PC 名（`%COMPUTERNAME%`、`KURIMA_LOG_SUFFIX` で上書き可）が入ります。

| ファイル | 内容 |
|---|---|
| `portal-run-<PC名>.log` | 実行ログ（サーバー起動、ジョブ・CLI の開始/終了、工程の進行） |
| `portal-error-<PC名>.log` | エラーログ（例外・traceback、ジョブ失敗の詳細のみ) |

どちらもサイズローテーション付きです（既定 5MB × 3 世代。`KURIMA_LOG_MAX_MB` /
`KURIMA_LOG_BACKUP_COUNT` で調整。世代は `portal-run-<PC名>.log.1` の形で残ります）。
また、リポジトリ内 `logs/` のジョブ詳細ログ・デバッグ出力は保持日数
`KURIMA_LOG_RETENTION_DAYS`（既定 30 日）を超えるとサーバー起動時に自動削除されます。

出力先フォルダは次の順で解決されます（実装: `portal_app/log_paths.py`）:

1. 環境変数 `KURIMA_LOG_DIR`（明示上書き用）
2. SharePoint 同期ライブラリ配下の `神里\くりまポータルエラーログ`
   （既定: `%USERPROFILE%\株式会社しまのや\くりまポータル - ドキュメント\神里\くりまポータルエラーログ`。フォルダが無ければ自動作成）
3. リポジトリ内 `logs/`（同期フォルダが無い PC でも起動できるようにする fallback）

ユーザー名は `%USERPROFILE%` から解決されるため、PC ごとの設定は不要です。
従来どおり、ジョブごとの詳細ログ（`events.jsonl` / `summary.json`）はリポジトリ内 `logs/` にも
出力され、`/logs`・`/jobs` 画面から閲覧できます（既存機能はそのまま）。

## トラブルシュート

- **「保存先を自動検出できませんでした」** — SharePoint ライブラリの同期を確認するか、`.env` に `KURIMA_PORTAL_ROOT` を設定。`uv run python scripts/doctor.py` で候補パスを確認できます。
- **ブラウザが起動しない / NotImplementedError** — `uv run playwright install chromium` を実行。Chrome/Edge がインストール済みなら自動検出されます。
- **ジョブが失敗した** — 画面のエラー表示の下に日本語の対処ガイドと「詳細ログを見る」リンクが出ます。`/jobs` の該当行から「詳細ログ」を開くか、`/logs` で `execution_runs/` 配下の events.jsonl を確認。
- **「同じ処理（…）が実行中のため、新しく開始しませんでした」と出る** — 別タブ・別PCから同じ処理がすでに動いています（二重決済・二重取込の防止）。`/jobs` と進捗表示で完了を待ってから再実行してください。
- **.py を変更したのに反映されない** — uvicorn は `--reload` なしだと自動反映されません。`.\scripts\restart.bat` で再起動してください（既存プロセスの停止→新しいウィンドウで起動まで自動。`restart.bat 8010` でポート指定、`restart.bat 8006 lan` で LAN 公開モード）。再起動忘れは画面フッターの「稼働版」と `/health` の `restart_required` で確認でき、`update.bat /restart` なら更新→再起動を一括実行します。
- **他のPCから繋がらない** — ホストPCのファイアウォール受信許可（上記）と、`-Mode lan` で起動しているか（`0.0.0.0` で LISTEN しているか）を確認。
- **「このパソコンからの利用は許可されていません」（403）と出る** — ホストPCの `.env` の `KURIMA_ALLOWED_CLIENTS` に、画面に表示されたIPアドレスを追加して再起動してください（設定を消せば従来どおり無制限）。
- **実行中にページを閉じて進捗が見えなくなった** — ジョブは裏で動き続けています。同じ画面を開き直せば進捗パネルへ自動で再接続され、ナビ「実行履歴」のバッジでも実行中件数を確認できます。

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
