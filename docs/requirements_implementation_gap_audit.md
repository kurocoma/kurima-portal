# 要件定義書・詳細設計書 ⇔ 実装 差異一覧（アクセス解析取得／請求関連取得）

## 文書情報

- 作成日: 2026-07-13
- 対象文書:
  - 要件定義書 `portal_tool/docs/access_analytics_billing_cards_requirements.md`（以下「要件」）
  - 詳細設計書 `portal_tool/docs/access_analytics_billing_cards_design.md`（以下「設計」）
- 対象実装:
  - `portal_app/services/access_analytics.py` / `access_analytics_rakuten.py` / `access_analytics_yahoo.py`
  - `portal_app/services/billing_statements.py` / `billing_statements_rakuten.py` / `billing_statements_yahoo.py`
  - `portal_app/services/paths.py` / `error_hints.py`
  - `portal_app/main.py`（ルーティング）、`portal_app/templates/`（画面）
- 調査方法: 文書の各節（機能要件・非機能要件／1.画面設計／2.ルーティング設計／3.サービス層設計／
  4.データ設計／5.外部連携設計／6.エラーハンドリング・ログ設計）を、**実装コードを実際に読んで**
  突き合わせた。表記ゆれ等の些末な差は数えず、**実装挙動に影響する差異のみ**を列挙する。

## サマリ

| # | 対象 | 区分 | どちらが正か | 対処 |
|---|---|---|---|---|
| 1 | 認証方式（自動ログイン） | 要件・設計 | 実装 | 要件・設計を更新（本ターン実施） |
| 2 | BillPay ログインURL | 設計 | 実装 | 設計を更新（本ターン実施） |
| 3 | Yahoo!請求 確定状態の取得箇所 | 設計 | 実装 | 設計を更新（本ターン実施） |
| 4 | BillPay 18か月切替後の `/reload` | 設計に記載なし | 実装 | 設計へ追記（本ターン実施） |
| 5 | BillPay 発行日の日本語形式 | 設計に記載なし | 実装 | 設計へ追記（本ターン実施） |
| 6 | 楽天RMS ラジオの装飾span | 設計 | 実装 | 設計を更新（本ターン実施） |
| 7 | 楽天アクセス解析の device キー | 設計 | 実装 | 設計を更新（本ターン実施） |
| 8 | Yahoo!全体分析の device キー | 設計 | 実装 | 設計を更新（本ターン実施） |
| 9 | Yahoo!請求 raw ファイル名 | 設計 | 実装 | 設計を更新（本ターン実施） |
| 10 | staging（正規化層）が未実装 | 実装 | **要件** | 要件未達。別チケットへ引き継ぎ（本ターン未対処） |
| 11 | 確定状態の状態数（3 vs 4） | 要件 | 実装 | 要件を更新（本ターン実施） |
| 12 | ルーティング（非JSフォールバック・download） | 設計 | 実装 | 設計を更新（本ターン実施） |
| 13 | error_hints の state 網羅 | 設計・実装の双方 | 両方 | 設計へ現状を明記。ヒント追加は別チケット（本ターン未対処） |
| 14 | BillPay `execute=False` が0件 | 実装 | 設計 | **実装を修正**（本ターン実施） |
| 15 | Yahoo!アクセス解析 `execute=False` の幻の警告 | 実装 | 設計 | **実装を修正**（本ターン実施） |
| 16 | UI「保存先」表示が `staging_dir` | 実装 | 設計 | 別チケットへ引き継ぎ（本ターン未対処） |

---

## 1. 認証方式：実装は自動ログインする（要件・設計は「自動入力しない」）

- **文書の記述**:
  - 要件「非機能要件」4: 「認証情報（ID・パスワード）は自動入力しない。初回ログイン・追加認証・
    二段階認証は人が行い、自動化はログイン後の画面遷移・CSV取得のみを担当する」
  - 設計 5.2: 「永続Chromeプロファイル（`launch_persistent_context`）＋**人手ログイン**」
- **実装の実際**: 4サービス全てが環境変数の認証情報でログインフォームを自動入力する。
  - `access_analytics_rakuten.py` `_attempt_rakuten_login()`
    （R-Login: `KURIMA_RAKUTEN_KANRI_LOGIN_ID` / `KURIMA_RAKUTEN_KANRI_PASSWORD`。
    後続の楽天会員認証ステップで `KURIMA_RAKUTEN_LOGIN_ID` / `KURIMA_RAKUTEN_LOGIN_PASSWORD`）
  - `access_analytics_yahoo.py` / `billing_statements_yahoo.py` `_attempt_yahoo_login()`
    （`KURIMA_YAHOO_LOGIN_ID` / `KURIMA_YAHOO_LOGIN_PASSWORD`）
  - `billing_statements_rakuten.py` `_attempt_billpay_login()`
    （`KURIMA_BILLPAY_LOGIN_ID` / `KURIMA_BILLPAY_LOGIN_PASSWORD`）
  - `access_analytics_yahoo.py` の当該定数直上コメントに
    「自動ログイン（2026-07-12 ユーザー許可により追加。…既存方針から本フローに限り意図的に逸脱する）」
    と根拠が明記されている。環境変数が未設定なら何もせず戻る（＝人手ログインへ縮退）。
- **どちらが正か**: **実装**。ユーザーの明示許可のもとで意図的に追加され、2026-07-13 に
  4サービス全てで自動ログイン＋CSV実取得の成功を確認済み。
- **対処**: 要件「非機能要件」4 と設計 5.2 を実装に合わせて更新した（本ターン実施）。

## 2. BillPay ログインURL

- **文書の記述**: 設計 3.4 画面・URL契約表および 5.1 対象URLまとめ:
  `login` = `https://billpay.rakuten.co.jp/rmssspartner/`
- **実装の実際**: `billing_statements_rakuten.py`
  `LOGIN_URL = "https://billpay.rakuten.co.jp/login"`。直上コメントに
  「2026-07-13、ユーザーの実操作で `/login`（短縮URL）が正しい入口だと確認された。
  `/rmssspartner/` は同じログインフォームへ辿り着くが、ユーザー環境では挙動が異なると
  報告されたため、確認済みの直URLを正本とする」と根拠あり。
- **どちらが正か**: **実装**（実接続で確認済み）。
- **対処**: 設計 3.4 / 5.1 の表を `/login` に更新し、`/rmssspartner/` は旧経路として注記した。

## 3. Yahoo!請求関連：確定状態は精算明細画面からのみ取得できる

- **文書の記述**: 設計 3.3「確定状態判定（順序が重要）」は `statusText` を各明細画面から
  取得できる前提の擬似コードのみを示し、**どの画面に確定状態表示があるか**を書いていない。
- **実装の実際**: `billing_statements_yahoo.py` の `_statement_state()` の docstring に
  「確定状態の表示は**精算明細（clearing）画面の締め日行にのみ存在する**。請求明細（demand）・
  受取明細（receive）の画面には「確定」「未確定」の表示が無い」と明記され、実装も
  `CLEARING_PATH`（`/amount/clearing?targetYm=...`）の締め日行だけを読み、そこで得た状態を
  請求・受取へ引き当てている。
- **どちらが正か**: **実装**（2026-07-13 の実接続で判明した実サイト仕様）。
- **対処**: 設計 3.3 に「確定状態の取得元は精算明細画面のみ」である旨を追記した。

## 4. BillPay：18か月切替で URL に `/reload` が付く

- **文書の記述**: 設計 3.4 の画面・URL契約表は `settlement_result` =
  `https://billpay.rakuten.co.jp/settlement_result` の固定URLのみを示し、画面遷移後の
  URL変化に言及していない。
- **実装の実際**: `billing_statements_rakuten.py` の `_assert_screen()` 付近に
  「`/settlement_result` → `/settlement_result/reload` のようにサフィックスが付く」と
  コメントがあり、パス完全一致ではなく前方一致で画面検証している。
- **どちらが正か**: **実装**。パス完全一致で検証すると、期間select（18か月）切替後に
  誤って `NEEDS_LOGIN` と判定してしまう。
- **対処**: 設計 3.4 の 18か月・pagination契約へ追記した。

## 5. BillPay：精算回の発行日は日本語形式（ラベルなし）

- **文書の記述**: 設計 3.4 は「`issue_date`: CSV内部の発行日を正本として採用（filenameは
  正本にしない）」とのみ記載し、**画面側の精算回列挙で発行日をどう読むか**を書いていない。
- **実装の実際**: `billing_statements_rakuten.py` の精算回パーサに
  「実UIの発行日は「2026年7月3日」という日本語形式で、tbody先頭に（ラベルなしで）出る」旨の
  コメントがあり、正規表現
  `(20\d{2})\s*[/\-年]\s*(\d{1,2})\s*[/\-月]\s*(\d{1,2})\s*日?` で日本語・スラッシュ双方を
  受けて ISO 形式へ正規化している。
- **どちらが正か**: **実装**（実画面の観測事実）。
- **対処**: 設計 3.4 へ追記した。

## 6. 楽天RMS：ラジオボタンが装飾 span に覆われ `check()` が失敗する

- **文書の記述**: 設計 3.1 の自動化フロー手順3:
  「`input[type="radio"][value="pc"]` → `sdApp` → `sdWeb` の順に切替え」
  （＝素直に `check()` できる前提）。
- **実装の実際**: `access_analytics_rakuten.py` の `_select_device()` に
  「RMSのラジオは装飾用の `<span class="rms-check-box">` に覆われており、Playwrightの
  `check()` は『span intercepts pointer events』で失敗する」と明記され、
  **通常の `check()`（短いタイムアウト）→ `label[for=...]` 経由クリック → `check(force=True)`**
  の3段フォールバックを実装している。
- **どちらが正か**: **実装**（実接続で判明）。
- **対処**: 設計 3.1 手順3 に、装飾span とフォールバック順序を追記した。

## 7. 楽天アクセス解析：保存される device キーは `app` / `smartphone_web`

- **文書の記述**: 設計 3.1 の dataclass:
  `device: str  # "pc" | "sdApp" | "sdWeb" | "all"`
- **実装の実際**: `sdApp` / `sdWeb` は **RMS画面のラジオ value**（`DEVICE_OPTIONS`）に過ぎず、
  dataclass `RakutenDeviceAccessCsv.device` と manifest に保存される device キーは
  正規化後の `pc` / `app` / `smartphone_web` / `all`（`access_analytics_rakuten.py` の
  正規化マップ、および `_read_saved_result` の `wanted = {"pc", "app", "smartphone_web"}`）。
  実 manifest（`data/access_analytics/manifest.jsonl`）でも `"device": "app"` /
  `"smartphone_web"` が保存されている。
- **どちらが正か**: **実装**（画面value と保存キーを分離するのは妥当）。
- **対処**: 設計 3.1 に「画面value と保存device キーは別物」である旨を追記した。

## 8. Yahoo!全体分析：保存される device キーは `smartphone_web`（画面 value は `sp`）

- **文書の記述**: 設計 3.2 の dataclass:
  `device: str  # "pc" | "sp" | "app" | "all"`
- **実装の実際**: `access_analytics_yahoo.py` の `DEVICE_BUTTONS` は
  `(セレクタ, 画面hidden value, 保存キー, ラベル)` の4つ組であり、
  `(".buttons-device_smt", "sp", "smartphone_web", "スマートフォンWeb")` のとおり
  **保存キーは `smartphone_web`**（`sp` は画面側の hidden value）。実 manifest でも
  `"device": "smartphone_web"` が保存されている。
- **どちらが正か**: **実装**（楽天側の device キーと語彙を揃えられる）。
- **対処**: 設計 3.2 の dataclass コメントを更新した。

## 9. Yahoo!請求関連：raw ファイル名に確定状態が入る

- **文書の記述**: 設計 4.2 ファイル命名規約:
  「請求関連（Yahoo）: `<statement_type>_<target_month:YYYYMM>.csv`
  （`offset_YYYYMM.csv` / `billing_YYYYMM.csv` / `receipt_YYYYMM.csv` は画面命名をそのまま踏襲）」
- **実装の実際**: `billing_statements_yahoo.py` `_commit_pending()`:
  `destination = paths.raw_dir / f"{pending.statement_type}_{pending.state}_{logical}"`
  → 実際は **`<statement_type>_<state>_<論理名>`**（例 `settlement_final_...`）。
  `statement_type` の値は `settlement` / `billing` / `receipt` であり、
  設計が書く `offset_` という接頭辞は使われていない（`offset` は画面上のCSV名）。
  なお設計 3.3 の保存キー節では「論理名に `final` / `provisional` / `unknown` を含める」と
  書かれており、**設計内部でも 3.3 と 4.2 が食い違っている**。
- **どちらが正か**: **実装**（確定状態をファイル名に含める 3.3 の方針が正しい。
  同一月を未確定→確定で再取得したとき、状態がファイル名で判別できる）。
- **対処**: 設計 4.2 の命名規約を実装に合わせて更新し、3.3 との食い違いを解消した。

## 10. staging（正規化層）が実装されていない ※要件未達・本ターン未対処

- **文書の記述**:
  - 要件「機能要件」出力形式: 「元CSVをそのまま保存する raw と、**分析アプリ向けに列を
    正規化した staging**（UTF-8 BOM付き）の2層」「正規化スキーマ（long形式含む）へ変換する
    staging を分離する」
  - 設計 4.2: 「raw＝取得bytesを変更せず保存」「**staging＝正規化**」「quarantine＝隔離」の3層
- **実装の実際**: `paths.py` は `staging_dir` を定義しているが、実装での用途は
  **ダウンロード作業用の一時領域**である（4サービスとも `batch_dir = paths.staging_dir / batch_id`
  にダウンロードし、検証通過後に `paths.raw_dir` へ `replace()`/`move()` で確定する。
  成功後は `batch_dir` を `rmdir()` する）。**列を正規化した staging 層・long形式変換は
  どこにも実装されていない**（`raw_dir /` への書き込みのみが存在する）。
- **どちらが正か**: **要件**（実装が要件を満たしていない）。
- **対処**: **本ターンでは未対処**。正規化スキーマの定義（列名・キー・long形式）自体が
  未確定であり、新規モジュールの追加を伴うため、本ターンの変更計画のスコープ外とした。
  別チケットとして引き継ぐ。現時点で raw CSV は保存・検証・ダウンロードともに機能しており、
  分析アプリ側は raw を読む前提であれば運用可能。

## 11. 確定状態の状態数：要件は3状態、実装は4状態

- **文書の記述**: 要件「機能要件」カード2 確定状態の扱い:
  「未確定（provisional）／確定（final）／不明（unknown）」の**3状態**を判定できることを必須要件とする
- **実装の実際**: `billing_statements.py` `_VALID_STATES = {"final", "provisional", "unknown", "no_data"}`
  の**4状態**。`no_data`（対象月に明細が存在しない）は、`unknown`（＝判定に失敗した）と
  区別する必要があるため追加されている（`billing_statements_yahoo.py` `_overall_state()`、
  および `_read_saved_result` が `state not in {"provisional", "no_data"}` のときだけ
  「ファイルがありません」警告を出す分岐で実際に使われる）。設計 1.2 のワイヤーフレームは
  既に `final/provisional/unknown/NO_DATA` の4状態を書いている。
- **どちらが正か**: **実装**（「データが無い」と「判定できない」は運用上まったく別物）。
- **対処**: 要件の当該行を4状態に更新した。

## 12. ルーティング：非JSフォールバックPOST 4本と、download の {mall} パラメータ化

- **文書の記述**: 設計 2.1 / 2.2 のエンドポイント表は
  `GET /access-analytics/download/rakuten/{artifact_id}` と
  `GET /access-analytics/download/yahoo/{artifact_id}` を**モール別に2本**列挙し、
  `POST /*/start` 系のみを実行系として挙げている。
- **実装の実際**: `main.py`
  - ダウンロードは `GET /access-analytics/download/{mall}/{artifact_id}` と
    `GET /billing/download/{mall}/{artifact_id}` の**各1本にパラメータ化**されている。
  - 設計の表に無い**非JSフォールバック用の同期POST が4本**ある:
    `POST /access-analytics/rakuten`、`POST /access-analytics/yahoo`、
    `POST /billing/yahoo`、`POST /billing/rakuten`。いずれも対応する `/start` を呼んでから
    303 リダイレクトする（JS無効環境でもカードを実行できるようにするため）。
- **どちらが正か**: **実装**（パラメータ化は等価かつ簡潔。フォールバックPOSTは既存カードと同じ作法）。
- **対処**: 設計 2.1 / 2.2 の表を実装のルート一覧に合わせて更新した。

## 13. error_hints：設計の state 表と実装が食い違う ※一部未対処

- **文書の記述**: 設計 6.1 は 11 個の state（`NEEDS_LOGIN` / `AUTH_REQUIRED` /
  `PAGE_CONTRACT_CHANGED` / `PAGINATION_STALLED` / `SCHEMA_MISMATCH` / `SCHEMA_DRIFT` /
  `NOT_FINALIZED` / `MONTH_UNAVAILABLE` / `DOCUMENT_TYPE_NOT_ALLOWED` /
  `AMBIGUOUS_DOWNLOAD` / `DOWNLOAD_FAILED` / `SESSION_RENEWAL_REQUIRED`）を挙げる。
- **実装の実際**: 4サービスが実際に `raise ... state="..."` する値は 18 種類あり、
  設計の表にも `error_hints.py` のヒント辞書にも**無い** state が6つある:
  `CONFIG_MISSING` / `DATA_NOT_UPDATED` / `PROFILE_IN_USE` / `EMPTY_ENTERPRISE_GROUP` /
  `ORPHAN_SHOP_ROW` / `ROW_ROLE_AMBIGUOUS`。これらが発生すると画面には日本語の対処ガイドが
  出ず、生の例外メッセージだけが表示される（設計 6.1 の「未知のエラーは `None` のまま」に
  形式上は合致するが、**自前で定義した state に自前のヒントが無い**のは設計意図と食い違う）。
  逆に `NOT_FINALIZED` は `error_hints.py` にヒントが定義済みだが、実装はこの state を
  一度も raise しない（未確定は例外にせず `provisional` として記録しスキップする方式に変わったため）。
  なお `AUTH_REQUIRED_MANUAL` は実装・ヒント双方にあるが設計の表に無い。
- **どちらが正か**: **両方に不足**。実装側は6 state 分のヒントが足りず、設計側は表が古い。
- **対処**: 設計 6.1 の表を現在の実装に合わせて更新し、「ヒント未定義の6 state」を明記した。
  **`error_hints.py` へのヒント追加は本ターンでは未対処**（本ターンの変更計画に
  `error_hints.py` が含まれていないため）。別チケットへ引き継ぐ。

## 14. BillPay `execute=False` が取得済み文書0件を返す（実装バグ・本ターン修正）

- **文書の記述**: 設計 3.4 / 2.2 は `execute=False`（dry-run）が保存済み manifest と帳票を
  検証して返すことを前提としている（`skipped_reason="dry-run: 保存済みmanifestと帳票のみ
  検証しました。"`）。
- **実装の実際（修正前）**: `billing_statements_rakuten.py` `_read_saved_result()` の
  `batch_candidates` 抽出フィルタに **`category` 条件が無かった**。このため
  `batch_complete` マーカー（`issue_date=None` / `artifact_id=None`）が
  同一 `batch_id` / `mall` / `screen` / `document_type` を持つがゆえに帳票候補へ混入し、
  続く `scope == "latest"` の
  `latest = max(str(record.get("issue_date", "")) for record in candidates)` が
  `str(None) == "None"` を最大値として選んでいた（`"N"`=0x4E > `"2"`=0x32 の文字列比較）。
  その結果、後段の `record.get("issue_date") == latest` が
  実在帳票（`"2026-07-03"`）でもマーカー（`None`）でも False となり、
  **candidates が空 → documents 0件**（警告すら出ない）になっていた。
  実 manifest（`data/billing_statements/manifest.jsonl`）で再現を確認:
  修正前 `documents: 0` / `max(str(issue_date)) == "None"`。
- **どちらが正か**: **設計**（実装のバグ）。実取得・実保存は成功していた。
- **対処**: **実装を修正した**（本ターン実施）。
  1. `batch_candidates` のフィルタに `record.get("category") == "billpay_document"` を追加。
  2. 多重防御として、`scope="latest"` の最新判定を `issue_date` が実在するレコードだけで行う。
  修正後、同じ実 manifest で `documents: 1`（issue_date=2026-07-03, row_count=4,
  validated=True）を確認。回帰テストは `tests/test_saved_result_manifest.py`。
- **補足（仕様上の制約。バグではない）**: `complete_markers` は `record.get("scope") == scope` で
  絞るため、`scope="latest"` で取得したバッチを `scope="date"` で読み戻すことはできない
  （0件になる）。batch_complete マーカーが「どの scope で完了したバッチか」を属性として
  持つ以上、これは一貫した挙動である。日付指定で読み戻したい場合は
  `scope="date"` で取得したバッチが必要。

## 15. Yahoo!アクセス解析 `execute=False` が実体のない警告を出す（実装バグ・本ターン修正）

- **文書の記述**: 同上（dry-run は保存済み成果物の検証のみを行う）。
- **実装の実際（修正前）**: `access_analytics_yahoo.py` `_read_saved_result()` の `records`
  抽出フィルタにも **`category` 条件が無かった**（#14 と同一のバグパターン）。
  `batch_complete` マーカーが `mall` / `target_label` / `account_fingerprint` を満たすため
  候補に混入し、`latest[(category, device)]` のキーが `("batch_complete", "None")` になった上で
  `_safe_manifest_path()` が `relative_path=None` により `None` を返し、
  **`batch_complete/None の保存済みファイルが見つかりません。`** という実体のない警告を
  毎回出していた（直前セッションで実際に観測された警告文と一致）。
  データ欠落は起きない（後段の `category` 分岐で `continue` されるため）が、
  利用者には「何かが欠けている」と誤認させる。
- **どちらが正か**: **設計**（実装のバグ）。
- **対処**: **実装を修正した**（本ターン実施）。`records` の抽出に
  `record.get("category") in {"product", "overall"}` を追加。回帰テストで、修正前に出ていた
  警告文が消えることを固定した。
- **横展開の確認結果（修正不要だった2モジュール）**:
  - `access_analytics_rakuten._read_saved_result()`: `device in wanted`
    （`{"pc","app","smartphone_web"}`）で絞っており、マーカーの `device=None` は
    `str(None)=="None"` となり allowlist に入らないため**元から混入しない**。修正不要。
  - `billing_statements_yahoo._read_saved_result()`: `record.get("statement_type") in requested_types`
    で絞っており、マーカーの `statement_type=None` は候補に入らないため**元から混入しない**。修正不要。
  - いずれも回帰テストを追加し、将来 allowlist の絞り込みを外した場合に検知できるようにした。

## 16. UI の「保存先」表示が `staging_dir` を指している ※本ターン未対処

- **文書の記述**: 設計 1.2（結果表示）は取得済みファイルの一覧表示を想定している。
- **実装の実際**: `access_analytics.py` / `billing_statements.py` のプレビュー生成が
  `"staging_dir": str(find_*_paths().staging_dir)` を返し、テンプレート
  （`_access_analytics_results.html` / `_billing_results.html` ほか）が
  これを **`<dt>保存先</dt>`** として表示している。しかし #10 のとおり実ファイルの確定先は
  `raw_dir` であり、`staging_dir` 配下の作業ディレクトリは成功時に削除される。
  結果として、画面に出る「保存先」パスは、同じ画面に列挙されているファイルが実際には
  存在しない場所を指す。
- **どちらが正か**: **設計**（実装の不整合）。表示すべきは `raw_dir`。
- **対処**: **本ターンでは未対処**。修正には `access_analytics.py` / `billing_statements.py`
  またはテンプレートの変更が必要で、本ターンの変更計画の対象ファイルに含まれていないため。
  別チケットへ引き継ぐ（#10 の staging 正規化層の実装と併せて扱うのが妥当）。

---

## 差異が無いことを確認した主な項目（実装コードを読んで確認）

以下は文書と実装が一致していたため、差異として計上していない。

- 対象モール・取得帳票種別（要件 機能要件）: 楽天RMS商品ページ分析3端末／Yahoo!商品分析＋全体分析4種／
  Yahoo!精算・請求・受取／BillPay `document-type` 34・33。実装の `DEVICE_OPTIONS` /
  `DEVICE_BUTTONS` / `_normalise_types` / `DOCUMENT_TYPE_ALLOWLIST` と一致。
- 取得粒度（要件）: 楽天アクセス解析は日次1日、Yahoo!請求は年月指定、BillPay は
  `latest` / `date` / `all`。実装の各 `_normalise_*` / `_validate_request` と一致。
- サービス層のモジュール分割（設計 3 冒頭の6ファイル構成）: 実装と完全一致。
- dataclass のフィールド構成（設計 3.1〜3.4 の「2026-07-12 実装後の追記」を含む）:
  `RakutenDeviceAccessCsv` / `YahooProductAccessResult` / `YahooStoreOverallCsv` /
  `YahooStatementFile` / `BillPayDocument` / 各 `*Result` を実装と突き合わせ、
  device キー（#7・#8）以外の差異は無し。
- 保存先ルート（設計 4.1）: `paths.py` の `AccessAnalyticsPaths` / `BillingStatementsPaths` と
  `find_*_paths()`、env 上書き（`KURIMA_ACCESS_ANALYTICS_DIR` /
  `KURIMA_BILLING_STATEMENTS_DIR`）は設計どおり。
- 外部URL（設計 5.1）: 楽天アクセス解析 `datatool.rms.rakuten.co.jp/access/item`、
  Yahoo! `pro.store.yahoo.co.jp/pro.{storeAccount}` 系（`/sales_manage/overall`,
  `/sales_manage/item_report`, `/amount/{clearing|demand|receive}?targetYm=`）、
  BillPay `/settlement_result`・`/billing_check` は実装の定数と一致（ログインURLのみ #2）。
- 監査ログの出力先（設計 6.2）: 4サービスの `_AUDIT_PATH` が `logs/access_analytics/` /
  `logs/billing_statements/` 配下の専用 JSONL を指し、金額・実ID・社名を書かない
  （件数・状態・SHA-256・ファイル名のみ）ことをコードで確認。
- 進捗ジョブの工程定義（設計 6.3）: `main.py` の `steps=[...]` は設計の3例と一致
  （楽天アクセス解析 / Yahoo!請求 / BillPay）。Yahoo!アクセス解析のみ設計に例が無く、
  実装は `login_check` / `fetch_product` / `fetch_overall` / `validate_save`。
- 機密の非出力（要件 非機能要件1〜3）: manifest の allowlist
  （`access_analytics.append_access_analytics_manifest` / `billing_statements.append_billing_manifest`）が
  金額・実ID・社名のキーを落とし、保存先が `portal_tool/data/` 配下（SharePoint 同期外）である
  ことをコードで確認。
