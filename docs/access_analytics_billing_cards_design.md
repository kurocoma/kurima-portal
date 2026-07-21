# アクセス解析取得・請求関連取得カード 詳細設計書

## 文書情報

- 対応要件: `portal_tool/docs/access_analytics_billing_cards_requirements.md`
- 種別: 詳細設計書（実装準備。本ドキュメント自体はコード変更を含まない）
- 作成日: 2026-07-12

## 参照した一次情報

### Obsidian vault（`C:/Users/hppym/dev/obsidian-vault`）

- `40-dev-notes/分析アプリ/楽天市場-デバイス別アクセス数取得手順.md`
- `40-dev-notes/分析アプリ/Yahoo!ショッピング-デバイス別アクセス数取得手順.md`
- `40-dev-notes/分析アプリ/Yahoo!ショッピング-精算・請求・受取明細取得手順.md`
- `40-dev-notes/分析アプリ/Yahoo!ショッピング-精算請求受取-Playwright実装仕様.md`
- `40-dev-notes/分析アプリ/楽天市場-BillPay精算データ取得手順.md`
- `40-dev-notes/分析アプリ/楽天市場-BillPay精算データ-Playwright実装仕様.md`
- `分析アプリ.md`（索引）

### 既存 `portal_tool` 実装

- `portal_tool/portal_app/main.py`（既存カードのルーティング規約）
- `portal_tool/portal_app/templates/dashboard.html`（`tool-card` のHTML構造）
- `portal_tool/portal_app/services/next_engine_yamato.py`（Playwright起動・dataclass・監査ログのパターン）
- `portal_tool/portal_app/services/inventory.py`（dataclass・サービス関数のパターン）
- `portal_tool/portal_app/services/progress_jobs.py`（非同期ジョブ・進捗パネルの仕組み）
- `portal_tool/portal_app/services/paths.py`（保存先パス解決の既存方式）
- `portal_tool/portal_app/services/b2_chrome.py`（専用永続Chromeプロファイルのパターン）
- `portal_tool/portal_app/services/error_hints.py`（例外→日本語対処ガイド変換）
- `portal_tool/portal_app/services/execution_logger.py`（実行ログのJSON安全化・機密キーのマスク）
- `portal_tool/portal_app/settings.py`（env設定カタログ・`mask_secret`）
- `portal_tool/.gitignore`（`data/` 全体・`*.csv`/`*.pdf` 等が除外済みであることの確認）

## 1. 画面設計

### 1.1 `dashboard.html` への `tool-card` 追加案

既存の `tool-card` は `tool-icon`（インラインSVG）・`tool-name`・`tool-desc`・`tool-meta`・
`tool-open` の5要素で構成される（`portal_tool/portal_app/templates/dashboard.html` 20〜51行目）。
新規2枚も同じ構造で `.tool-grid` 内に追加する。

```html
<a class="tool-card" href="/access-analytics">
  <span class="tool-icon"><!-- 棒グラフ系のインラインSVG（stroke #147f9f、既存アイコンと同トーン） --></span>
  <span class="tool-name">アクセス解析取得</span>
  <span class="tool-desc">楽天市場・Yahoo!ショッピングのデバイス別・商品別アクセス数を取得します。</span>
  <span class="tool-meta">楽天RMS商品ページ分析／Yahoo!商品分析・全体分析CSV取得</span>
  <span class="tool-open">開く<!-- 既存の矢印SVG --></span>
</a>

<a class="tool-card" href="/billing">
  <span class="tool-icon"><!-- 明細書系のインラインSVG --></span>
  <span class="tool-name">請求関連取得</span>
  <span class="tool-desc">Yahoo!精算・請求・受取明細、楽天BillPay精算データを取得します。</span>
  <span class="tool-meta">月次精算・請求・受取明細／BillPay店舗別内訳書CSV取得</span>
  <span class="tool-open">開く<!-- 既存の矢印SVG --></span>
</a>
```

### 1.2 新規ページのワイヤーフレーム

**`/access-analytics`**（`inventory.html`/`yamato.html` と同様、Jinja2テンプレート1枚）

```
┌ ページ見出し「アクセス解析取得」──────────────────┐
│ [タブ] 楽天市場 | Yahoo!ショッピング                 │
│                                                      │
│ 楽天市場タブ:                                        │
│   対象日 [日付入力] （既定=前日）                     │
│   [取得実行]ボタン → 進捗パネル（ログイン確認／PC取得／│
│     楽天市場アプリ取得／スマートフォン取得／検証保存） │
│   結果: 3件のCSVリンク＋行数・SHA-256（先頭8桁）表示   │
│                                                      │
│ Yahoo!ショッピングタブ:                               │
│   対象期間 [開始日]-[終了日]（既定=前日のみ）          │
│   [取得実行]ボタン → 進捗パネル（ログイン確認／商品分析│
│     取得／全体分析4件取得／検証保存）                  │
│   結果: 商品分析1件＋全体分析4件のリンク・行数表示      │
└──────────────────────────────────────────────────┘
```

**`/billing`**（同様に1テンプレート＋タブ）

```
┌ ページ見出し「請求関連取得」────────────────────────┐
│ [タブ] Yahoo!ショッピング | 楽天市場（BillPay）        │
│                                                      │
│ Yahoo!タブ:                                          │
│   対象年月 [YYYY-MM]  帳票種別 [精算/請求/受取 複数選択]│
│   確定済みのみ取得 [チェックボックス]（既定ON）          │
│   [取得実行]ボタン → 進捗パネル（ログイン確認／月選択／  │
│     確定状態判定／各帳票ダウンロード／検証保存）         │
│   結果: 帳票ごとの状態（final/provisional/unknown/     │
│     NO_DATA）・行数・SHA-256を一覧表示（金額等は非表示） │
│                                                      │
│ 楽天BillPayタブ:                                      │
│   取得範囲 [最新1件 / 発行日指定 / 全件(18か月)]         │
│   画面 [確定済み精算 / 事前確認]                       │
│   [取得実行]ボタン → 進捗パネル（ログイン確認／18か月表示│
│     ／精算回列挙／document取得／検証保存）              │
│   結果: 精算回ごとの状態・帳票種別・SHA-256一覧          │
│     （社名・金額・実IDは非表示、既存 settings 同様マスク）│
└──────────────────────────────────────────────────┘
```

両画面とも、既存 `/inventory`・`/yamato` と同じ「GET は即時表示、重いプレビューは
`/*/preview` からの遅延ロード」パターン（`main.py` 476〜551行目の `_inventory_response` /
`inventory_preview` 相当）を踏襲する。ここでの「重いプレビュー」は前回実行結果の再表示
（ファイル一覧・manifest読み込み）であり、実際の外部サイトアクセスは行わない。

## 2. ルーティング設計

既存カードは「GET 画面表示」「GET `/preview` 遅延ロード」「POST `/*/start` 非同期実行
（`progress_jobs.start` が `job_id` を返す）」「GET `/progress/{job_id}` ポーリング（共通・
追加不要）」「GET `/*/download/...` ダウンロード」というパターンに統一されている
（例: `/inventory`, `/inventory/preview`, `/inventory/fetch-next-engine/start`,
`/inventory/download/{kind}`）。新規2カードも同じ形にする。

### 2.1 アクセス解析取得カード

| メソッド・パス | 対応する既存パターン | 内容 |
|---|---|---|
| `GET /access-analytics` | `GET /inventory` | 画面表示（`defer_preview=True`） |
| `GET /access-analytics/preview` | `GET /inventory/preview` | 前回取得結果（manifest）の遅延表示 |
| `POST /access-analytics/rakuten/start` | `POST /inventory/fetch-next-engine/start` | 楽天3端末CSV取得ジョブ開始。フォーム: `target_date` |
| `POST /access-analytics/yahoo/start` | 同上 | Yahoo!商品分析＋全体分析4件取得ジョブ開始。フォーム: `period_start`, `period_end` |
| `GET /access-analytics/download/{mall}/{artifact_id}` | `GET /inventory/download/{kind}` | 保存済みCSVのダウンロード（**2026-07-13 訂正**: モール別2本ではなく `{mall}` パラメータの1本で実装されている） |
| `POST /access-analytics/rakuten` | 既存カードの非JSフォールバック | **2026-07-13 追記**: JS無効環境向け。`/access-analytics/rakuten/start` を呼んだ後 303 で `/access-analytics` へリダイレクトする |
| `POST /access-analytics/yahoo` | 同上 | 同上（`/access-analytics?tab=yahoo` へリダイレクト） |

### 2.2 請求関連取得カード

| メソッド・パス | 対応する既存パターン | 内容 |
|---|---|---|
| `GET /billing` | `GET /yamato` | 画面表示（`defer_preview=True`） |
| `GET /billing/preview` | `GET /yamato/preview` | 前回取得結果（manifest）の遅延表示 |
| `POST /billing/yahoo/start` | `POST /shipment-confirmation/fetch-yamato/start` | Yahoo!精算/請求/受取取得ジョブ開始。フォーム: `target_month`, `types`（複数）, `final_only` |
| `POST /billing/rakuten/start` | 同上 | 楽天BillPay取得ジョブ開始。フォーム: `screen`（settlement_result/billing_check）, `scope`（latest/date/all）, `issue_date` |

> **2026-07-12 追記（前回evalフィードバック6）**: `download_billpay_settlement` の
> `document` 引数（既定 `"settlement-shop-csv"`）はUIから公開しておらず、
> `summary-csv`（表示情報CSV）や `doctype-32`/`doctype-41` 等の他allowlist帳票は
> 現行UIから選択できない。サービス層（`DOCUMENT_TYPE_ALLOWLIST`）は将来の詳細設定UI
> 追加を見据えて意図的に広く受け付ける設計であり、UIへの露出は本設計のスコープ外
> （`billing_statements_rakuten.py` の `DOCUMENT_TYPE_ALLOWLIST` 直上コメントも参照）。
| `GET /billing/download/{mall}/{artifact_id}` | `GET /inventory/takaesu/download` | 保存済み帳票のダウンロード（**2026-07-13 訂正**: モール別2本ではなく `{mall}` パラメータの1本で実装されている） |
| `POST /billing/yahoo` | 既存カードの非JSフォールバック | **2026-07-13 追記**: JS無効環境向け。`/billing/yahoo/start` を呼んだ後 303 で `/billing` へリダイレクトする |
| `POST /billing/rakuten` | 同上 | 同上（`/billing?tab=rakuten` へリダイレクト） |

`/progress/{job_id}`・`/progress/{job_id}/cancel`・`/progress/active` は既存の共通APIをそのまま
使う（`main.py` 958〜1010行目）。追加実装は不要。

いずれの `/*/start` も `progress_jobs.start(..., workflow="access_analytics_rakuten" 等)` の
`workflow` 名を新設し、既存の `DuplicateJobError` による二重実行ガード（S3）をそのまま適用する。

## 3. サービス層設計

既存コードは「モール横断の集約モジュール（例: `yamato_b2_workflow.py` が
`next_engine_yamato.py` と `yamato_b2_import.py` を束ねる）」と「モール別・機能別の実装モジュール」
に分割する構成が多い。新規2カードも同じ方針で分割する。

```
portal_app/services/
├── access_analytics.py            # 集約: main.py が import する入口
├── access_analytics_rakuten.py    # 楽天RMS商品ページ分析CSV取得の実装
├── access_analytics_yahoo.py      # Yahoo!商品分析・全体分析CSV取得の実装
├── billing_statements.py          # 集約: main.py が import する入口
├── billing_statements_yahoo.py    # Yahoo!精算・請求・受取明細取得の実装
└── billing_statements_rakuten.py  # 楽天BillPay精算データ取得の実装
```

### 3.1 `access_analytics_rakuten.py`

`next_engine_yamato.py` の `NextEngineYamatoClient` 相当のクラス構成にする。

> **2026-07-12 実装後の追記（前回evalフィードバック5）**: 以下のコード例は
> 設計当初案のまま残し、実装済みコードとの差分をここに記載する。
> `header_columns` は設計案では「期待値28」を表す `int` としていたが、
> 実装では検証済みヘッダー実体を返す `tuple[str, ...]`（28要素）にした
> （呼び出し側が実際のヘッダー文字列を検査できるようにするため）。
> 他のフィールド名・`RakutenDeviceAccessResult` は設計どおり一致している。
>
> **2026-07-13 実接続後の追記**: `device` フィールドの値は
> **`"pc"` / `"app"` / `"smartphone_web"`（+ `"all"`）** である。
> 下のコード例にある `sdApp` / `sdWeb` は **RMS画面のラジオ value**
> （`DEVICE_OPTIONS` のキー）であって、dataclass・manifest に保存する device キーではない。
> 実装は画面value → 保存キーの正規化マップ（`sdApp`→`app`, `sdWeb`→`smartphone_web`）を
> 持ち、Yahoo!側（3.2）と語彙を揃えている。

```python
@dataclass(frozen=True)
class RakutenDeviceAccessCsv:
    device: str  # "pc" | "sdApp" | "sdWeb" | "all"
    target_date: date
    downloaded_file: Path
    source_sha256: str
    row_count: int          # ヘッダー(6行)除く商品データ行数
    header_columns: int     # 期待値28（実装では tuple[str, ...] 。上記追記を参照）

@dataclass(frozen=True)
class RakutenDeviceAccessResult:
    executed: bool
    target_date: date
    csv_files: tuple[RakutenDeviceAccessCsv, ...]  # pc/sdApp/sdWeb（+all任意）
    skipped_reason: str | None
    warnings: tuple[str, ...]


async def download_rakuten_device_access(
    *, execute: bool, target_date: date,
    include_all: bool = False,
    headless: bool | None = None, slow_mo_ms: int = 0,
) -> RakutenDeviceAccessResult: ...

def download_rakuten_device_access_sync(...) -> RakutenDeviceAccessResult:
    return asyncio.run(download_rakuten_device_access(...))
```

自動化フロー（ノート「楽天市場-デバイス別アクセス数取得手順.md」の擬似コードをそのまま実装へ
落とす）:

1. 専用永続Chromeプロファイル（後述 5.2）で `https://datatool.rms.rakuten.co.jp/access/item` を開く。
2. `input[type="radio"][value="daily"]` で日次を選択し、`input[data-toggle="daterangepicker"]`
   （`readonly`）のカレンダーUIで対象日1日を選ぶ（`fill()` 不可のため、
   `.daterangepicker .drp-calendar.left/right td.available:not(.off)` をクリックし `決定`）。
3. `input[type="radio"][value="pc"]` → `sdApp` → `sdWeb` の順に切替え、都度
   「読み込み中です。しばらくお待ちください。」の非表示を待つ。
   **2026-07-13 実接続で判明**: RMSのラジオは装飾用の `<span class="rms-check-box">` に
   覆われており、Playwright の `check()` は "span intercepts pointer events" で失敗する。
   実装（`_select_device()`）は **(a) 通常の `check()`（短いタイムアウト）→
   (b) `label[for="<radio id>"]` 経由のクリック → (c) `check(force=True)`** の
   3段フォールバックで切り替える。`check()` 単発の実装にしてはならない。
4. `全商品CSV`（role=button）→ ダイアログ（role=dialog, 見出し `CSVダウンロード`）→
   `すべての項目`（role=radio）・`全件`（role=radio）→ `ダウンロード` で開始。
5. `expect_download()`／`download.save_as()` を優先しつつ、ダウンロード先ディレクトリの差分監視
   （`.tmp` を確定ファイルとして扱わない）をフォールバックとして実装する。
6. CSVの5行目（端末名）・3行目（期間）・6行目（28列ヘッダー）を検証してから raw へ確定する。

検証項目（ノート「必須バリデーション」を転記）: UTF-8としてエラーなく読める／3行目の期間が
要求日と一致／5行目の端末が要求端末と一致／6行目28列ヘッダーが期待値と一致／
`商品管理番号` が空でない・重複しない／`アクセス人数`・`ユニークユーザー数` が0以上の整数／
`.tmp` を取り込まない／0件時は「本当に0件」か「未更新・ログイン切れ・画面変更」かを別判定する。

保存キー（staging）: `shop_id + target_date + 商品管理番号 + device`。

### 3.2 `access_analytics_yahoo.py`

> **2026-07-12 実装後の追記（前回evalフィードバック5）**: 実装済みコードの
> 実際の命名に合わせて更新する。`YahooProductAccessResult` は
> `executed: bool` を持たず（`executed` は `YahooAccessAnalyticsResult` 側のみ）、
> 代わりに `device: str` と `header_columns: tuple[str, ...]` を持つ。
> `YahooStoreOverallCsv` にも `period_start` / `period_end` / `row_count` /
> `header_columns` が追加されている。`YahooAccessAnalyticsResult` は
> `product_access` → `product`、`store_overall` → `overall` に改名され、
> `period_start` / `period_end` を自身も持つ。
>
> **2026-07-13 実接続後の追記**: `YahooStoreOverallCsv.device` に保存する値は
> **`"pc"` / `"smartphone_web"` / `"app"` / `"all"`** である（下のコード例の `"sp"` は
> 画面側の hidden value）。実装の `DEVICE_BUTTONS` は
> `(セレクタ, 画面hidden value, 保存キー, ラベル)` の4つ組であり、
> `(".buttons-device_smt", "sp", "smartphone_web", "スマートフォンWeb")` のとおり
> 画面value と保存キーを分離している（楽天側 3.1 と語彙を揃えるため）。

```python
@dataclass(frozen=True)
class YahooProductAccessResult:
    device: str  # 商品分析はデバイス列を持たないため "unspecified" 固定
    period_start: date
    period_end: date
    downloaded_file: Path
    source_sha256: str
    row_count: int  # ヘッダー除く
    header_columns: tuple[str, ...]

@dataclass(frozen=True)
class YahooStoreOverallCsv:
    device: str  # "pc" | "sp" | "app" | "all"
    period_start: date
    period_end: date
    downloaded_file: Path
    source_sha256: str
    row_count: int
    header_columns: tuple[str, ...]

@dataclass(frozen=True)
class YahooAccessAnalyticsResult:
    executed: bool
    period_start: date
    period_end: date
    product: YahooProductAccessResult | None       # 旧案 product_access
    overall: tuple[YahooStoreOverallCsv, ...]        # 旧案 store_overall（pc/sp/app/all の4件）
    skipped_reason: str | None
    warnings: tuple[str, ...]


async def download_yahoo_access_reports(
    *, execute: bool, period_start: date, period_end: date,
    headless: bool | None = None, slow_mo_ms: int = 0,
) -> YahooAccessAnalyticsResult: ...
```

観測済み識別子（ノートから転記。憶測で識別子を追加しない）:

| 対象 | 識別子／値 |
|---|---|
| 全体分析 | `/sales_manage/overall` |
| 商品分析 | `/sales_manage/item_report` |
| 日次開始日 | `#dailyInputDatepickerFrom` |
| 日次終了日 | `#dailyInputDatepickerTo` |
| 期間適用 | `#dailyInputApplyButton` |
| PC | `.buttons-device_pc` / hidden value `pc` |
| スマホWeb | `.buttons-device_smt` / hidden value `sp` |
| アプリ | `.buttons-device_app` / hidden value `app` |
| 合算値 | `.buttons-device_sum4` / hidden value `all` |
| 全体分析CSV | `#dataTableCsvDownload` |
| 商品分析CSV | `#itemReportCsvDownload` |

商品分析CSVはデバイス列を持たないため `device="unspecified"` として保存し、`all` とは扱わない
（ストア全体のデバイス比率を商品PVへ掛ける推計値は実測値として保存しない）。
全体分析4種の合計値は必ず Yahoo!側の `合算値` CSVを正とし、`PC + スマホWeb + アプリ` を独自の
合計値としては保存しない（3デバイス合計と合算値には差異が実測されている）。

保存キー（staging）: `product_access` は商品コード＋期間、`store_overall` は
`store_account + date + device`。

### 3.3 `billing_statements_yahoo.py`

> **2026-07-12 実装後の追記（前回evalフィードバック5）**: `YahooStatementFile` は
> 実装では常に確定した取得結果だけを表すため、`downloaded_file` /
> `source_sha256` / `row_count` はいずれも非null（`Path` / `str` / `int`）にした
> （未取得・未確定はmanifestの仮想レコードとして別経路で記録し、この
> dataclassのインスタンスにはしない）。`YahooBillingStatementsResult` は
> `final_only: bool` → `statement_state: str`（全体の確定状態サマリ）に改名し、
> `skipped_reason: str | None` を追加した。

```python
@dataclass(frozen=True)
class YahooStatementFile:
    statement_type: str      # "settlement" | "billing" | "receipt"
    target_month: str        # "YYYY-MM"
    settlement_closing_date: str | None  # settlementのみ
    statement_state: str     # "final" | "provisional" | "unknown" | "no_data"
    downloaded_file: Path
    source_sha256: str
    row_count: int

@dataclass(frozen=True)
class YahooBillingStatementsResult:
    executed: bool
    target_month: str
    statement_state: str     # 旧案 final_only（全体の確定状態サマリ）
    files: tuple[YahooStatementFile, ...]
    skipped_reason: str | None
    warnings: tuple[str, ...]


async def download_yahoo_statements(
    *, execute: bool, target_month: str,
    types: tuple[str, ...] = ("billing", "receipt", "settlement"),
    final_only: bool = True,
    headless: bool | None = None, slow_mo_ms: int = 0,
) -> YahooBillingStatementsResult: ...
```

URL契約（`Yahoo!ショッピング-精算請求受取-Playwright実装仕様.md` から転記）:

```
base = https://pro.store.yahoo.co.jp/pro.{storeAccount}
精算 /amount/clearing?targetYm=YYYYMM
請求 /amount/demand?targetYm=YYYYMM
受取 /amount/receive?targetYm=YYYYMM
```

確定状態判定（順序が重要。「未確定」に「確定」の文字列が含まれるため必ず未確定を先に判定）:

```python
if statusText.includes("未確定"): return "provisional"
if statusText.strip() == "確定": return "final"
return "unknown"
```

> **2026-07-13 実接続後の追記（確定状態の取得元）**: 確定状態の表示は
> **精算明細（`/amount/clearing`）画面の締め日行にのみ存在する**。
> 請求明細（`/amount/demand`）・受取明細（`/amount/receive`）の画面には
> 「確定」「未確定」の表示そのものが無い。したがって実装（`_statement_state()`）は
> **精算明細画面の締め日行からのみ確定状態を読み取り**、その状態を請求・受取にも引き当てる。
> 請求・受取の画面を見て確定判定しようとすると必ず `unknown` になるため、
> 「各明細画面に状態表示がある」前提で実装してはならない。
>
> また実装の状態は `final` / `provisional` / `unknown` に加えて **`no_data`**（対象月に
> 明細が存在しない）を持つ4状態である（「データが無い」と「判定に失敗した」を区別するため。
> 1.2 のワイヤーフレームと同じ）。

CSV契約: `CP932 / BOMなし / CRLF / comma / 前置きなし`。請求・受取ヘッダーは
`利用日,注文ID,利用項目,備考,金額（税抜き）,消費税,金額（税込）`。精算ヘッダーは
`利用日,注文ID,利用項目(請求),金額(請求：税抜き),消費税,金額(請求：税込),利用項目(受取),
金額(受取：税抜き),消費税,金額(受取：税込)` で、`消費税` 列が2つあるため**列名の辞書化は禁止**、
5列目を `billing_tax_yen`、9列目を `receipt_tax_yen` として列位置で読む。精算CSVの期待行数は
「表示件数−1」（小計行がCSVに含まれないため）。

ポップアップ・ダウンロード契約: `page.waitForEvent("popup")` → 「利用詳細ダウンロード」画面 →
`a[onclick^="download_file"]` を全件列挙して直列ダウンロード。全part検証（schema・対象月・
合計行数）が揃うまで raw へ確定しない（不合格は `quarantine/` へ）。

保存キー: `store_account + statement_type + target_month + settlement_closing_date + device`
は使わず、Yahoo!側は
`store_account + report_type + grain + period_start/date + period_end + statement_state` を基本とし、
論理名に `final` / `provisional` / `unknown` を含める（`FINAL_CONTENT_CHANGED` 等の判定は
5節のエラー分類を参照）。

### 3.4 `billing_statements_rakuten.py`

> **2026-07-12 実装後の追記（前回evalフィードバック5）**: `BillPayDocument` は
> `kind` → `document_kind` に改名し、`artifact_id: str` と `row_count: int | None`
> を追加、`issue_date` は非null（`str`。CSV内部の発行日を正本として採用する
> ロジックが確定日付を返すため）にした。`BillPaySettlementResult` は
> トップレベルの `issue_date` を廃止し（`scope=date` 時は呼び出し引数側で保持）、
> `settlement_summary_csv` という専用フィールドも廃止した
> （表示情報CSV=summaryも他の帳票と同様 `documents` タプルの1件として扱う設計に
> 統一したため）。代わりに `skipped_reason: str | None` を追加した。

```python
@dataclass(frozen=True)
class BillPayDocument:
    screen: str              # "settlement_result" | "billing_check"
    document_type: str       # "34" | "33" | "32" | ... （allowlistのみ）
    document_kind: str       # 旧案 kind。"csv" | "pdf" | "zip"
    issue_date: str          # CSV内部の発行日を正本として採用（filenameは正本にしない）
    artifact_id: str
    downloaded_file: Path
    source_sha256: str
    row_count: int | None
    validated: bool

@dataclass(frozen=True)
class BillPaySettlementResult:
    executed: bool
    screen: str
    scope: str                # "latest" | "date" | "all"
    documents: tuple[BillPayDocument, ...]  # summary（表示情報CSV）も1件として含む
    skipped_reason: str | None
    warnings: tuple[str, ...]


async def download_billpay_settlement(
    *, execute: bool, screen: str, scope: str,
    issue_date: str | None = None,
    document: str = "settlement-shop-csv",
    headless: bool | None = None, slow_mo_ms: int = 0,
) -> BillPaySettlementResult: ...
```

BillPayは既に「実装仕様」ノートに Python 参照kit（`billpay_download.py`）の詳細な状態機械・
CLI契約・exit code（0/2/3/4/5/6/7/8/9/70）まで定義済みである。本設計では、参照kitのロジックを
`billing_statements_rakuten.py` 内部モジュールへ移植し、`main.py` からは
`download_billpay_settlement_sync(...)` のような薄い関数として呼び出す構成にする
（参照kitをサブプロセスとして呼ぶ方式は、既存 `progress_jobs` の工程別進捗表示と相性が悪いため
採用しない。移植方針の詳細判断は次工程＝実装フェーズで確定する）。

画面・URL契約（転記）:

| screen key | URL |
|---|---|
| `login` | `https://billpay.rakuten.co.jp/login`（**2026-07-13 更新**。旧案の `/rmssspartner/` は同じログインフォームへ辿り着くが、ユーザー環境では挙動が異なると報告されたため、実操作で確認済みの短縮URLを正本とする。実装の `LOGIN_URL` も `/login`） |
| `home` | `https://billpay.rakuten.co.jp/home` |
| `settlement_result` | `https://billpay.rakuten.co.jp/settlement_result` |
| `billing_check` | `https://billpay.rakuten.co.jp/billing_check` |
| `billing_status` | `https://billpay.rakuten.co.jp/billing_status`（現時点は自動取得対象外。画面表のみで document key 未定義） |
| `payment_status` | `https://billpay.rakuten.co.jp/payment_status`（画面表のみ、download対象外） |

document-type allowlist（`settlement_result` の主取得対象は `34`＝店舗別内訳書CSV、
`billing_check` の主取得対象は `33`＝同CSV。他type（32/41/51/72/74/52/11/31）は必要時のみ）。
allowlist外のtypeは推測しない（`DOCUMENT_TYPE_NOT_ALLOWED` として拒否）。

18か月・pagination契約: `select#period` を18に設定 → 先頭ページの
`table.billpay_main_table > tbody:visible` のみを列挙（36精算回・1ページ4件・全9ページが観測値。
件数を固定値として強制しない）→ 上側 `ul.pagination` 内 `li[page-no="Next"] span[aria-label="Next"]`
を1回ずつクリックし、クリック前後で visible tbody の fingerprint（テキストのSHA-256）が
変化することを確認する。

> **2026-07-13 実接続後の追記（2件）**
>
> 1. **期間select（18か月）を切り替えると URL にサフィックスが付く**。
>    `https://billpay.rakuten.co.jp/settlement_result` →
>    `https://billpay.rakuten.co.jp/settlement_result/reload` に変化する。
>    このため画面検証（`_assert_screen()`）は **パス完全一致にしてはならない**。
>    完全一致で実装すると、18か月切替の直後に「別画面へ飛ばされた」と誤検知して
>    `NEEDS_LOGIN` を返す。実装は前方一致で `/settlement_result` 系を許容している。
> 2. **精算回の発行日は「2026年7月3日」という日本語形式で、ラベルなしで tbody 先頭に出る**
>    （`発行日:` のようなラベルは付かない）。実装は
>    `(20\d{2})\s*[/\-年]\s*(\d{1,2})\s*[/\-月]\s*(\d{1,2})\s*日?` で日本語形式・
>    スラッシュ形式の双方を受けて ISO（`YYYY-MM-DD`）へ正規化する。
>    `YYYY/MM/DD` 決め打ちのパーサでは1件も精算回を拾えない。

CSV契約（17列 店舗別内訳書、document-type 34/33）:

```
発行日,精算書No,店舗別内訳書No,店舗別ID,ＵＲＬ,店舗名,支払（税込額）,請求（税抜額）,
請求（税額）,支払/請求分類,集約科目,品目,精算対象期間開始日,精算対象期間終了日,
金額,うち消費税,税率
```

`ＵＲＬ` は全角列名であり、半角 `URL` へ自動修正しない。CP932 strict decode、BOMなし、CRLFのみ、
17列exact。単独 `-` はmissing、`-<digits>` は有効な負数として区別する。

12列 表示情報CSV（確定画面「表示情報のCSVダウンロード」）は
`企業ID,店舗名,店舗別ID,URL,精算書発行日,ご請求計算額,ご請求締め日,お支払計算額,お支払締め日,
ご精算額,お支払予定日,お支払期限日` で、`企業行 → 1件以上の店舗行` のgroup構造をパースする
（2行固定ペアと決め打ちしない。orphan店舗行・空group はエラー）。

セッション: 30分。25分チェックポイントで新規ダウンロードを開始せず、安全に停止・再開できる
状態（`state.json` 相当）を持つ。session切れは `NEEDS_LOGIN` として明確に判定し、0件・帳票なしと
混同しない。

保存キー: `screen_kind + settlement_fingerprint + document_type + content_sha256`。manifestや
ログでは精算書No・店舗別ID・URLを平文キーにしない。

### 3.5 集約モジュール（`access_analytics.py` / `billing_statements.py`）

`yamato_b2_workflow.py` の `prepare_yamato_b2_sync` に相当する、`main.py` から直接呼ばれる薄い
オーケストレーション層。役割は「複数モールの取得を1つの進捗ジョブ工程（`ProgressStep`列）に
束ねる」「manifestの読み書き」であり、モール固有のPlaywright操作は持たない。

```python
# access_analytics.py
def download_rakuten_device_access_sync(...) -> RakutenDeviceAccessResult: ...  # re-export
def download_yahoo_access_reports_sync(...) -> YahooAccessAnalyticsResult: ...  # re-export
def read_access_analytics_manifest() -> AccessAnalyticsManifest: ...           # /preview用
```

## 4. データ設計

### 4.1 保存先ルート

既存 `paths.py` の `PortalPaths` は SharePoint 同期のポータルルート配下（受注明細CSV・商品管理
シート等、スタッフ全員が閲覧する共有ライブラリ）を指す。要件定義書「非機能要件」で述べたとおり、
今回の2カードのraw/staging/manifestは**共有ライブラリに置かない**。代わりに、既存
`b2_chrome.py` の `default_profile_dir()`（`APP_ROOT / "data" / "b2_chrome_profile"`）と同じ
「`portal_tool/data/` 配下・Git管理外・env で上書き可」のパターンを踏襲する
（`.gitignore` で `data/` 全体と `*.csv`/`*.pdf` は既に除外済み）。

`paths.py` に次の2関数を追加する（既存 `find_portal_paths()` と対になる形）:

```python
@dataclass(frozen=True)
class AccessAnalyticsPaths:
    root: Path        # 既定 APP_ROOT/data/access_analytics
    raw_dir: Path      # root/raw
    staging_dir: Path  # root/staging
    manifest_path: Path  # root/manifest.jsonl

@dataclass(frozen=True)
class BillingStatementsPaths:
    root: Path         # 既定 APP_ROOT/data/billing_statements
    raw_dir: Path
    staging_dir: Path
    quarantine_dir: Path
    manifest_path: Path

def find_access_analytics_paths() -> AccessAnalyticsPaths:
    """KURIMA_ACCESS_ANALYTICS_DIR で上書き可。既定は APP_ROOT/data/access_analytics。"""

def find_billing_statements_paths() -> BillingStatementsPaths:
    """KURIMA_BILLING_STATEMENTS_DIR で上書き可。既定は APP_ROOT/data/billing_statements。"""
```

新規env変数は `settings.py` の `SETTINGS_CATALOG` へ既存グループの隣に追加する
（`KURIMA_ACCESS_ANALYTICS_DIR`, `KURIMA_BILLING_STATEMENTS_DIR`,
`KURIMA_ACCESS_ANALYTICS_CHROME_PROFILE`, `KURIMA_BILLPAY_CHROME_PROFILE`,
`KURIMA_YAHOO_STATEMENTS_CHROME_PROFILE` 等。BillPayの `expected_company_id` 等の期待値も
秘匿設定として `secret=True` グループに追加する）。

### 4.2 raw／staging分離と冪等性

Obsidianノートが定義する「raw＝取得bytesを変更せず保存」「staging＝正規化」「quarantine＝検証
不合格の隔離」の3層構造を、`portal_tool` 側でもそのまま採用する。既存コードの
`_next_yamato_file_path`（`next_engine_yamato.py` 1106行目）のような「保存先ディレクトリ＋
タイムスタンプ＋連番」による衝突回避パターンをベースに、次のファイル命名規約を追加する。

- アクセス解析（楽天）: `rakuten_item_access_<device>_<target_date:YYYYMMDD>.csv`
  （ノートの実例 `rakuten_item_access_pc_20260612_20260711.csv` は期間表記だが、分析アプリでは
  日次1日運用のため `_<target_date>` のみを使う）
- アクセス解析（Yahoo）: `yahoo_product_access_device-unspecified_<period_start>_<period_end>.csv`,
  `yahoo_store_overall_<device>_<period_start>_<period_end>.csv`
- 請求関連（Yahoo）: `<statement_type>_<state>_<論理名>`（**2026-07-13 実装に合わせて訂正**）。
  `statement_type` は `settlement` / `billing` / `receipt`、`state` は
  `final` / `provisional` / `unknown` / `no_data`。例: `settlement_final_...csv`。
  当初案の `offset_YYYYMM.csv` という接頭辞（`offset` は画面上のCSV名）は使っていない。
  確定状態をファイル名に含めるのは、同一月を未確定→確定で再取得したときに区別するため
  （3.3 の保存キー方針「論理名に final/provisional/unknown を含める」が正であり、
  旧 4.2 の記述はそれと矛盾していた）。
- 請求関連（楽天BillPay）: `billpay_<screen>_doctype-<type>_<artifact-id>.<ext>`
  （ノート推奨どおり、内部IDを露出させない正規化名。元filenameは機密manifestにのみ保持）

> **2026-07-13 追記（実装の現状。raw / staging の役割）**: 実装では
> `staging_dir` を **ダウンロード作業用の一時領域**として使っている
> （`batch_dir = staging_dir/<batch_id>` にダウンロード → 検証通過後に `raw_dir` へ確定移動 →
> 空になった `batch_dir` を削除）。**「列を正規化した staging 層」は未実装**であり、
> 確定した成果物は全て `raw_dir` にある。要件定義書が求める staging（分析アプリ向けの
> 正規化・long形式）は別チケットとして残っている。
> 詳細は `requirements_implementation_gap_audit.md` の #10 / #16 を参照。

### 4.2.1 manifest の `batch_complete` マーカー契約（2026-07-13 追記）

manifest（JSONL）には2種類のレコードが混在する。**読み出し側は必ず `category` で絞ること。**

| レコード種別 | `category` | 特徴 |
|---|---|---|
| 成果物レコード | `device_access` / `product` / `overall` / `statement` / `billpay_document` | `artifact_id` / `relative_path` / `sha256` を持つ |
| バッチ完了マーカー | `batch_complete` | `artifact_id` / `relative_path` / `sha256` はいずれも `None`。`scope="latest"` のとき `issue_date` も `None` |

マーカーは「この `batch_id` のバッチは完了した」という印であり、**成果物ではない**。
`category` で絞らずに `batch_id` だけで候補を集めると、マーカー自身が成果物候補に混入する。
実際に BillPay の `_read_saved_result()` でこれが起き、`scope="latest"` の
`max(str(record.get("issue_date", "")) ...)` が `str(None) == "None"` を最大値として拾い
（文字列比較で `"N"`(0x4E) > `"2"`(0x32)）、実在する帳票が全て振り落とされて
**`execute=False` が取得済み0件を返す**不具合になった（2026-07-13 修正済み）。
Yahoo!アクセス解析でも同じ混入により
`batch_complete/None の保存済みファイルが見つかりません。` という実体のない警告が出ていた
（同日修正済み）。回帰テスト: `tests/test_saved_result_manifest.py`。

冪等性キー（重複取得防止）: 楽天BillPayは
`account_fingerprint | document-type | issue-date`、Yahoo!請求関連は
`store_account + statement_type + target_month + settlement_closing_date`、アクセス解析は
`shop_id/store_account + target_date + device` を artifact identity とし、同一identityの
`content_sha256` が既存manifestと一致する場合はダウンロードをno-opにする
（`accepted-index.json` 相当を manifest 内に持たせる）。

### 4.3 必須バリデーション（転記・要約なし）

各サービス関数は、raw確定前に次を満たすことを検証する（満たさない場合はquarantineへ）。

- 楽天アクセス解析: UTF-8デコード可／3行目期間一致／5行目端末一致／6行目28列ヘッダー一致／
  `商品管理番号` 非空・重複なし／`アクセス人数`・`ユニークユーザー数` が0以上整数
- Yahooアクセス解析: 商品分析14列・全体分析24列のヘッダーexact一致／HTMLログイン画面でない／
  CP932で読める
- Yahoo請求関連: CP932 strict／BOMなし／CRLF／ヘッダー順序完全一致／全行の列数一致／
  非空の利用日が要求月内／landing表示件数とCSV行数の照合（精算は表示件数−1）
- 楽天BillPay: CP932 strict／BOMなし／CRLFのみ／17列または12列exact／`ＵＲＬ`列名を維持／
  企業行→店舗行のgroup構造検証（12列）／店舗別ID・URL・店舗名がTOML相当の期待値とexact一致

## 5. 外部連携設計

### 5.1 対象URLまとめ

| モール | 用途 | URL |
|---|---|---|
| 楽天 | アクセス解析 | `https://datatool.rms.rakuten.co.jp/access/item` |
| Yahoo | アクセス解析（全体） | `https://pro.store.yahoo.co.jp/pro.{storeAccount}/sales_manage/overall` |
| Yahoo | アクセス解析（商品） | `https://pro.store.yahoo.co.jp/pro.{storeAccount}/sales_manage/item_report` |
| Yahoo | 請求関連（利用明細） | `https://pro.store.yahoo.co.jp/pro.{storeAccount}/amount/{clearing|demand|receive}?targetYm=YYYYMM` |
| 楽天 | BillPayログイン | `https://billpay.rakuten.co.jp/login`（2026-07-13 更新。旧案 `/rmssspartner/`） |
| 楽天 | BillPay確定済み精算 | `https://billpay.rakuten.co.jp/settlement_result`（18か月切替後は `/settlement_result/reload`） |
| 楽天 | BillPay事前確認 | `https://billpay.rakuten.co.jp/billing_check` |

### 5.2 認証方式

既存 `b2_chrome.py` は「B2クラウドはPlaywright起動ブラウザに縮退応答を返すため実Chromeを
detached起動する」という特殊事情を持つが、今回の4フローはObsidianノート上ではいずれも
「Playwright管理下の永続Chromeプロファイル（`launch_persistent_context`）」で
問題なく動作することが検証済み（BillPay実装仕様ノート 3節）。したがって新規4フローは
`next_engine_downloader.py` の `storage_state` 方式ではなく、BillPayノートが指定する
`chromium.launch_persistent_context` 方式に統一する（アクセス解析2種・Yahoo!請求関連も同方式に
揃え、セッション切れ時の扱いを共通化する）。

> **2026-07-12 追加 / 2026-07-13 実接続で確認（自動ログイン）**: 当初の設計・要件定義書は
> Obsidianノートの方針に従い「認証情報は自動入力しない。初回ログイン・追加認証・二段階認証は
> 人が行う」としていたが、**ユーザーの明示許可により、本4フローに限り自動ログインを実装した**
> （要件定義書「非機能要件」4 も同日に更新済み）。2026-07-13 に4サービス全てで
> 自動ログイン＋CSV実取得の成功を確認している。
>
> | フロー | 実装関数 | 環境変数 |
> |---|---|---|
> | 楽天アクセス解析（RMS） | `_attempt_rakuten_login()` | `KURIMA_RAKUTEN_KANRI_LOGIN_ID` / `KURIMA_RAKUTEN_KANRI_PASSWORD`（R-Login）＋ 後続の楽天会員認証で `KURIMA_RAKUTEN_LOGIN_ID` / `KURIMA_RAKUTEN_LOGIN_PASSWORD` |
> | Yahoo!アクセス解析 | `_attempt_yahoo_login()` | `KURIMA_YAHOO_LOGIN_ID` / `KURIMA_YAHOO_LOGIN_PASSWORD` |
> | Yahoo!請求関連 | `_attempt_yahoo_login()` | 同上（Yahoo! JAPAN ID は共通） |
> | 楽天BillPay | `_attempt_billpay_login()` | `KURIMA_BILLPAY_LOGIN_ID` / `KURIMA_BILLPAY_LOGIN_PASSWORD` |
>
> 設計上の制約は維持する:
> - **環境変数が未設定なら自動ログインは何もせず戻る**（＝従来どおり人手ログインへ縮退する）。
>   認証情報の投入は運用者の任意。
> - 二段階認証・追加認証・CAPTCHA が出た場合は自動化せず、人手対応へフォールバックする。
> - 認証情報は `.env`（Git管理外）にのみ置き、ログ・manifest・進捗パネル・監査JSONLへ
>   出力しない（`execution_logger.SENSITIVE_KEY_PATTERN` は `password|login_id` 等を含む）。
> - 永続プロファイルは引き続き使用し、Cookie が生きていればログインフォーム自体に到達しない。

専用プロファイルディレクトリ（`b2_chrome.default_profile_dir()` と同じ設計。OS userのみ読める
権限にする）:

- `data/access_analytics_rakuten_chrome_profile/`
- `data/access_analytics_yahoo_chrome_profile/`
- `data/billing_statements_yahoo_chrome_profile/`
- `data/billpay_chrome_profile/`

いずれも複数プロセスからの同時起動を禁止する（BillPayノートの「profile lock」相当）。
`portal_app/services/execution_logger.py` の `SENSITIVE_KEY_PATTERN` は
`password|passwd|pwd|secret|security|token|cookie|authorization|credential|login_id` を
カバーするが、「金額」「企業ID」「店舗名」等の財務語は対象外である。したがって、これら4サービスは
`write_event`/`write_summary` へ渡す `data` を最初から「件数・状態・SHA-256・ファイル名」等の
非機密要約に絞り込み、実金額・実社名・実IDのフィールドをそもそも生成しない設計とする
（`execution_logger.py` 側のマスク辞書に頼らない。既存の `_append_audit`／`_append_product_audit`
（`next_engine_yamato.py`）が監査ログ用に整形済みdictだけを書き出すパターンと同じ考え方）。

### 5.3 Playwright操作契約（転記）

3.1〜3.4節に転記済みの識別子・URL・待機条件をそのまま実装の一次ソースとする。追加で
実装時に必要になる契約:

- 楽天アクセス解析: ダウンロード完了検知はイベント（`expect_download`/`download.save_as`）を
  優先しつつ、専用ディレクトリのファイルサイズ安定監視をフォールバックとして併用する
  （観測事実として `.tmp` → `YYYYMMDD_YYYYMMDD_item_list.csv` へのリネームが発生する）。
- Yahoo請求関連: `landing.waitForEvent("download")` 後 `download.failure()` を確認し、
  失敗時は `DOWNLOAD_FAILED` とする。
- 楽天BillPay: ダウンロードイベント欠落時は専用ディレクトリの差分watcherへフォールバックし、
  安定候補が複数なら `exit 6` 相当（`AMBIGUOUS_DOWNLOAD`）で停止し、任意に最新を選ばない。

## 6. エラーハンドリング・ログ設計

### 6.1 `error_hints.py` への追加方針

既存 `hint_for_exception` / `hint_for_message` は、型名・メッセージキーワードから日本語の対処文を
引く仕組みであり、`B2LoginError.state` のような属性ベースの分類にも対応している
（`error_hints.py` 17〜34行目 `_B2_STATE_HINTS`）。新規4サービスも同じ仕組みに載せるため、
専用の例外クラスに `state`（またはそれに準ずる属性）を持たせ、次のヒント辞書を追加する。

| state/コード | 由来 | 対処文の方向性 |
|---|---|---|
| `NEEDS_LOGIN` | アクセス解析2種・BillPay | 「ログインが切れています。専用ブラウザで再ログインしてから再実行してください」 |
| `AUTH_REQUIRED` | Yahoo請求関連 | 同上（Yahoo!側の呼称に合わせる） |
| `PAGE_CONTRACT_CHANGED` | BillPay（`select#period`欠落等） | 「画面構成が変わった可能性があります。開発担当に連絡してください（証跡を保存済み）」 |
| `PAGINATION_STALLED` | BillPay | 「一覧のページ送りが進みませんでした。時間をおいて再実行してください」 |
| `SCHEMA_MISMATCH` / `SCHEMA_DRIFT` | 全4サービス（列数・列名不一致） | 「CSVの形式が変わった可能性があります。開発担当に連絡してください」 |
| `NOT_FINALIZED` | Yahoo請求関連（`--final-only`相当） | 「対象月がまだ確定していません。確定後に再実行するか、確定済みのみのチェックを外してください」 |
| `MONTH_UNAVAILABLE` | Yahoo請求関連 | 「指定した年月は選択できません。年月を確認してください」 |
| `DOCUMENT_TYPE_NOT_ALLOWED` | BillPay | 「未対応の帳票種別です（実装対象外）」 |
| `AMBIGUOUS_DOWNLOAD` / `DOWNLOAD_FAILED` | 全4サービス | 「ダウンロードに失敗しました。時間をおいて再実行してください」 |
| `SESSION_RENEWAL_REQUIRED` | BillPay（25分チェックポイント） | 「セッションの有効期限が近いため安全に停止しました。再ログイン後、続きから再開してください」 |

未知のエラーは既存方針どおり `None`（画面は生メッセージのみ表示）のままとする。

> **2026-07-13 追記（実装との差異）**: 実装が実際に `raise ... state="..."` する値は上表より多く、
> 次の6つは **上表にも `error_hints.py` のヒント辞書にも無い**（発生しても日本語の対処ガイドが
> 出ず、生の例外メッセージだけが画面に出る）:
> `CONFIG_MISSING`（必須envの未設定）/ `DATA_NOT_UPDATED`（CSVが空）/
> `PROFILE_IN_USE`（専用プロファイルの多重起動）/ `EMPTY_ENTERPRISE_GROUP` /
> `ORPHAN_SHOP_ROW` / `ROW_ROLE_AMBIGUOUS`（いずれもBillPay 12列CSVのgroup構造検証）。
> 逆に `NOT_FINALIZED` はヒントが定義済みだが、実装はこの state を raise しない
> （未確定は例外にせず `provisional` として記録してスキップする方式に変わったため）。
> また `AUTH_REQUIRED_MANUAL`（自動ログインが通らず人手対応が必要）は実装・ヒント双方にあるが
> 上表に無い。ヒント追加は別チケット（`requirements_implementation_gap_audit.md` #13）。

### 6.2 `execution_logger.py` / 監査ログ

各サービスは `next_engine_yamato.py` の `_append_audit` 系関数と同じ形で、専用の監査JSONLへ
「実行時刻・実行種別・成否・状態・ファイル名・SHA-256・件数」のみを追記する。

- `logs/access_analytics/rakuten_access_audit.jsonl`
- `logs/access_analytics/yahoo_access_audit.jsonl`
- `logs/billing_statements/yahoo_statements_audit.jsonl`
- `logs/billing_statements/billpay_audit.jsonl`

`progress_jobs.start(..., workflow=...)` に渡す `metadata` にも実データ（金額・企業ID等）を
含めない（進捗スナップショットは `/progress/{job_id}` でLAN内から参照できるため）。

### 6.3 `progress_jobs.py` 統合（工程定義例）

```python
# アクセス解析（楽天）
steps=[("login_check", "ログイン確認"), ("fetch", "3端末CSV取得"), ("validate_save", "検証・保存")]

# 請求関連（Yahoo）
steps=[("login_check", "ログイン確認"), ("select_month", "対象月選択・確定状態判定"),
       ("fetch", "帳票ダウンロード"), ("validate_save", "検証・保存")]

# 請求関連（楽天BillPay）
steps=[("login_check", "ログイン確認"), ("list_settlements", "18か月表示・精算回列挙"),
       ("fetch", "帳票ダウンロード"), ("validate_save", "検証・保存")]
```

いずれも `workflow` 名を一意にし（例: `access_analytics_rakuten`, `billing_statements_yahoo`,
`billing_statements_rakuten`）、既存の二重実行ガード（`DuplicateJobError`）をそのまま適用する。

## 7. セキュリティ設計

> **2026-07-12 追記（ユーザー許可による方針変更）**: 下記2.の「4フローとも自動入力を
> 行わない」は設計当初の方針だったが、2026-07-12にユーザーが明示的に許可したため、
> 4フロー全てにログイン画面リダイレクト検知時のみ動作する自動ログイン機能
> （環境変数 `KURIMA_RAKUTEN_LOGIN_ID`/`PASSWORD`、`KURIMA_YAHOO_LOGIN_ID`/`PASSWORD`、
> `KURIMA_BILLPAY_LOGIN_ID`/`PASSWORD` からの読み取り＋Playwrightフォーム入力）を
> 追加した。既存の`NEXT_ENGINE_LOGIN_ID`パターンと同じ「`.env`にID/パスワードを持たせる」
> 方式を、本4フローに限り採用する。認証情報は関数ローカル変数としてのみ扱い、
> ログ・監査JSONL・manifest・例外メッセージ・戻り値のdataclassには一切含めない
> （`execution_logger.py`の`SENSITIVE_KEY_PATTERN`は新規6変数名を追加なしで
> マスク対象に含む。`login_id`・`password`トークンに一致するため）。
> 2段階認証等の追加認証は自動突破せず`AUTH_REQUIRED_MANUAL`として停止し、
> 人手でのログインを要求する（「追加認証は人が行う」方針は維持）。
> 実装詳細・実接続確認の結果は`turn-000-report.md`を参照。

1. **保存先の分離**: 4.1節のとおり、raw/staging/manifest/quarantineはすべて
   `portal_tool/data/` 配下（`.gitignore` で除外済み）に置き、SharePoint同期のポータルルート
   （既存の共有ライブラリ）には一切書き込まない。
2. **認証情報の非保存 → 2026-07-12 方針変更（上記追記参照）**: 設計当初は4フローとも
   自動入力を行わず、初回・セッション切れ時のログインは人が行う方針だった
   （BillPayノート「自動化の境界」表をそのまま踏襲）。ユーザー許可により、本4フローに
   限り自動ログインへ方針変更している。
3. **ログのマスク**: 5.2節のとおり、監査ログ・進捗スナップショットへは非機密要約のみを渡す。
   `settings.py` の `mask_secret()` と同様の考え方で、BillPayの期待値（`expected_company_id` 等）
   を設定する場合は `SETTINGS_CATALOG` へ `secret=True` として追加し、`/settings` 画面でも
   マスク表示にする。
4. **アクセス制御**: 既存の `KURIMA_ALLOWED_CLIENTS` ミドルウェア（`main.py` 153〜179行目
   `_restrict_clients`）は新規ルートにも自動的に適用される（パス単位の除外をしていないため）。
   追加のパス単位認可は本設計のスコープ外（要件定義書「未確定事項」参照）。
5. **Git管理**: `portal_tool/.gitignore` は `data/` 全体、`*.csv`/`*.pdf`/`*.png` 等を既に除外
   しているため、新規ディレクトリを追加してもコミット対象にならないことを実装時に
   `git status` で確認する。
6. **read-only原則**: 4フローとも「取得のみ」を実装範囲とし、支払・登録・状態変更操作は行わない
   （BillPayノート「自動化の境界」表の「対象外」に一致）。

## 8. 実装ステップ案

1. `paths.py` に `AccessAnalyticsPaths` / `BillingStatementsPaths` と解決関数を追加し、
   `settings.py` の `SETTINGS_CATALOG` へ対応するenvキー（保存先・Chromeプロファイル・
   BillPay期待値）を追加する。
2. `access_analytics_rakuten.py` を実装する（日次1端末→3端末の順でPlaywrightフローを固め、
   raw保存・検証を先に通す）。
3. `access_analytics_yahoo.py` を実装する（商品分析→全体分析4件の順）。
4. `access_analytics.py`（集約）・`/access-analytics` 系ルーティング・テンプレート・
   `dashboard.html` への `tool-card` 追加を行う。
5. `billing_statements_yahoo.py` を実装する（帳票1種→3種の順、確定状態判定を先に固める）。
6. `billing_statements_rakuten.py` を実装する（BillPay参照kitの移植方針を確定させたうえで、
   `scope=latest` の最小フロー→18か月全件列挙の順に拡張する。複雑度が高いため独立ステップとする）。
7. `billing_statements.py`（集約）・`/billing` 系ルーティング・テンプレート・
   `dashboard.html` への `tool-card` 追加を行う。
8. `error_hints.py` へ6.1節のヒント辞書を追加し、監査ログ・進捗ジョブ統合を仕上げたうえで
   コードレビューを行う（本ドキュメントのDoDとは別に、実装フェーズのDoDは着手時に定める）。

各ステップは既存カード（`/inventory`, `/yamato`, `/shipment-confirmation`）のPRサイズ感に合わせ、
1ステップ＝1レビュー単位を想定する。
