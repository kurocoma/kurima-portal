# Claude Design 依頼プロンプト: アクセス解析取得・請求関連取得 画面デザイン

## 文書情報

- 対象: kurima-portal（`portal_tool`）に新規追加する `/access-analytics`（アクセス解析取得）・
  `/billing`（請求関連取得）の2画面
- 種別: Claude Design（UIデザイン担当）へそのまま渡す依頼プロンプト。本文書自体はコード実装を
  含まない（HTML/CSSの新規作成はこの文書のスコープ外）
- 作成日: 2026-07-12
- 前提資料（この依頼の一次情報）:
  - `portal_tool/docs/access_analytics_billing_cards_requirements.md`（要件定義書）
  - `portal_tool/docs/access_analytics_billing_cards_design.md`（詳細設計書。特に
    「1. 画面設計」節・「2. ルーティング設計」節）

以下、Claude Design への依頼文です。

---

## 1. 目的・背景

kurima-portal は、プログラムの知識がない現場スタッフ・店舗運営担当者が、業務端末（LAN内）の
ブラウザから「青い大きなボタンを押すだけ」で日次・月次の定型作業を進めるための業務ポータルです
（`C:/Users/hppym/dev/obsidian-vault/90-setup/kurima-portal-manual.md` 冒頭より：
「プログラムの知識は要りません。上のメニューから業務を選び、青い大きなボタンを押すだけで…自動で
進めます。」）。現在は「在庫明細確認」「ヤマト伝票」「クリックポスト」「出荷確定」の4業務が
ポータルトップの `tool-card` から利用できます。

今回追加する「アクセス解析取得」（`/access-analytics`）「請求関連取得」（`/billing`）の2画面も、
**同じ利用者・同じ運用文脈**（現場スタッフがマウス操作だけで完結させる、専門知識前提にしない）で
使われます。デザインもこれに合わせ、既存4業務と地続きの見た目・操作感にしてください。

## 2. 対象2画面の要件

対象は次の2画面です。両画面とも `base.html` を継承した1つの Jinja2 テンプレートに、タブで
モール（取得先）を切り替える構成です。以下は詳細設計書「1.2 新規ページのワイヤーフレーム」から
そのまま転記した機能配置の確定案です（要約していません）。

### 2.1 `/access-analytics`（アクセス解析取得）

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

### 2.2 `/billing`（請求関連取得）

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

両画面とも、既存 `/inventory`・`/yamato` と同じ「GETは即時表示、重いプレビューは `/*/preview`
から遅延ロード（`data-lazy` スロット。`base.html` が共通で処理）」パターン、「POST `/*/start` で
非同期ジョブを開始し、既存の `#progress-panel` / `progress.js` による進捗表示（実行中/完了/失敗の
状態）で結果を追う」パターンを踏襲します。各タブの「取得実行」ボタンが、そのタブの唯一の主操作
です。

### 2.3 UIデザインとして追加で検討してほしい観点

上記のワイヤーフレームは機能配置の確定案ですが、**見た目の細部（階層・余白・状態表現）は
未確定**です。次の観点で具体的な提案をしてください（ワイヤーフレームの箱組みをそのまま図として
清書するだけでは不十分です）。

- **視覚的階層**: ページ内で「まず目に入るべき要素」（タブ→フォーム→実行ボタン→進捗→結果）の
  優先順位をどう表現するか。
- **情報の優先順位**: 特に `/billing` の結果一覧は「状態（final/provisional/unknown/NO_DATA）・
  行数・SHA-256」など複数の情報を持つため、どれを先に見せ、どれを折りたたむか。
- **余白**: 既存 `.panel` / `.tbl` / `.filecard` 等の余白感を踏襲しつつ、新規画面特有の要素
  （複数選択の帳票種別、確定済みのみチェック等）を詰め込みすぎない（情報密度を上げすぎない）配置。
- **レスポンシブ**: 既存CSSは860px・560pxのブレークポイントを持ちます（`styles.css` の
  `@media (max-width: 860px)` / `@media (max-width: 560px)`）。タブ・フォーム項目・結果一覧が
  狭幅でも崩れない配置にしてください。
- **状態表示のわかりやすさ**: 進捗中/完了/エラーに加え、この2画面特有の状態（`final`/
  `provisional`/`unknown`、`NEEDS_LOGIN` 等の再ログイン要求）を、現場スタッフが一目で区別できる
  見せ方。

## 3. 既存デザインシステム（流用が前提）

新しい配色・新しいコンポーネントは増やさず、既存の `portal_tool/portal_app/static/styles.css` の
デザイントークンとコンポーネントをそのまま流用してください。**理由**: 現場スタッフは既存3〜4業務
と同じ見た目・同じ操作パターンで迷わず使えることを最優先しており、新規2画面だけ配色や部品が違うと
「別のツール」に見えてしまい、現場での学習コストが上がります。

主要トークン（`styles.css` の `:root` より抜粋）:

```css
--bg: #f4f2ee;              /* ページ背景 */
--surface: #ffffff;         /* カード・パネル背景 */
--primary: #147f9f;         /* ブランドカラー（＝マニュアルの「青い大きなボタン」） */
--ok: #2f7d57;   --ok-bg: #eaf4ee;   --ok-line: #c5e4d2;   /* 完了・成功 */
--warn: #8a5e12; --warn-bg: #fbf4e6; --warn-line: #ecdcbf; /* 要確認 */
--error: #a23b3b; --error-bg: #fdf1f1; --error-line: #ecc9c9; /* 失敗 */
--font: "Noto Sans JP", "Yu Gothic UI", "Hiragino Kaku Gothic ProN", "Meiryo", system-ui, sans-serif;
--shadow-card: 0 1px 2px rgba(80, 55, 25, .04);
--shadow-hover: 0 10px 26px rgba(20, 127, 159, .11);
--maxw: 1040px;
```

既存の `tool-card`（ポータルトップの業務カード。`portal_tool/portal_app/templates/dashboard.html`
より、実際に使われている構造）:

```html
<a class="tool-card" href="/inventory">
  <span class="tool-icon"><svg ...></svg></span>
  <span class="tool-name">在庫明細確認</span>
  <span class="tool-desc">受注明細から、発注確認用の集計を作成します。</span>
  <span class="tool-meta">通常商品・選べるセットの集計／CSV出力</span>
  <span class="tool-open">開く<svg ...></svg></span>
</a>
```

`appbar`（`base.html` の共通ヘッダー・ナビゲーション枠）は既に `/access-analytics` `/billing` の
両ルートに対応済みの想定です（本依頼ではナビの追加・変更は行いません。5節参照）。

新規2画面のテンプレートは、既存の再利用可能なCSSクラスの組み合わせだけでレイアウトを構成する
ことを基本とし、`styles.css` へ新しいクラス・トークンを追加しないでください。参考になる既存
クラス（`inventory.html` で実際に使われている組み合わせ）:

- `.tabbar` / `.tablink`（モール切替タブ。`inventory.html` の「通常の明細」「高江洲発注明細」と
  同じ考え方）
- `.panel` / `.panel.center`（フォーム＋実行ボタンの主パネル）
- `.flow` / `.flow-chip`（丸バッジ①②③の工程プレビュー。`.flow-chip.is-active` /
  `.is-done` / `.is-failed` で状態を色分け）
- `.progress-panel` / `.progress-steps` / `.progress-step`（実行後の進捗パネル。既存
  `#progress-panel` の仕組みをそのまま利用）
- `.field` / `.field-row` / `.input` / `.select`（対象日・対象期間・対象年月などのフォーム項目）
- `.opt`（確定済みのみ取得のようなチェックボックス項目）
- `.tbl` / `.tbl-wrap` / `.filecard` / `.file-grid`（結果一覧・CSVリンク表示）
- `.note.ok` / `.note.warn` / `.note.error`（状態別の注意表示）
- `.badge` / `.badge.primary`（final/provisional/unknown 等の状態タグ）

既存クラスの組み合わせだけでは表現しきれない要素に気づいた場合は、実装を先取りせず「この部分は
新規クラスの追加が必要そうです」という提案としてデザイン説明に書き添えてください。

## 4. シンプルさの制約（運用文脈に基づく）

`kurima-portal-manual.md` が示す運用方針（「プログラムの知識は要りません」「青い大きなボタンを
押すだけ」）に基づき、次を具体的な制約とします。

- 専門用語を画面の主導線に出さない。`SHA-256`、`manifest`、`quarantine`、`workflow` のような開発者
  向け語は、既存 `/inventory` の「行数」「確認事項」のような現場向けの言い換えに寄せる（詳細な
  技術情報は既存パターンと同様、折りたたみ内に置くのは可）。
- 1画面（1タブ）の主要操作を1つに絞る。各タブに「取得実行」ボタンは1つだけとし、既存
  `.btn-cta` のような大きな主ボタンとして目立たせる。
- 進捗パネルは既存の丸バッジ①②③パターン（`.flow-chip` ＋ `.progress-panel`）をそのまま踏襲し、
  新しい進捗表現を作らない。
- 情報密度を上げすぎない。特に `/billing` は帳票種別・確定状態・SHA-256など表示項目が多いため、
  一覧の初期表示は要点（状態・件数）に絞り、詳細は既存 `.fold`（詳細情報の折りたたみ）に収める。
- 業務端末はPCが主ですが、レスポンシブ対応（2.3節）を踏まえ、将来的なタブレット等での片手操作・
  現場の明るい/暗い照明下でも状態（成功・要確認・失敗）が視認性よく区別できることを意識する。
  誤操作防止（同じ取得実行ボタンの連打対策等）は既存 `.btn-cta:disabled` の考え方を踏襲する。

## 5. 技術的な制約

- 技術スタック: FastAPI + Jinja2 + 素のCSS（フレームワークなし。npm/CDN追加なし）。
- テンプレート構成: `base.html` を継承した1テンプレート構成。`/access-analytics` は新規
  `access_analytics.html` 1枚、`/billing` は新規 `billing.html` 1枚とし、それぞれの中でタブ切替
  により両モールを扱う（既存 `inventory.html` の `{% set current_tab = active_tab|default(...) %}`
  ＋ `tabbar` パターンを参照）。
- 変更してよい範囲: `portal_app/templates/` 配下への新規テンプレート2ファイルの追加のみ。
- 変更してはいけない範囲（対象外）: 既存の `/inventory`, `/yamato`, `/clickpost`,
  `/shipment-confirmation` の各テンプレート・`dashboard.html`・`styles.css` には手を入れない。
  `dashboard.html` への `tool-card` 追加案は詳細設計書「1.1」で既に確定しているため、本依頼の
  デザイン対象外とする。
- 必ず読んでほしい参照ファイル:
  - `portal_tool/portal_app/templates/base.html`（appbar・nav・進捗表示の共通枠）
  - `portal_tool/portal_app/templates/dashboard.html`（`tool-card` の実HTML構造）
  - `portal_tool/portal_app/templates/inventory.html`（タブ・フォーム・進捗パネル・結果表示の
    実際の組み方）
  - `portal_tool/portal_app/static/styles.css`（デザイントークン・既存コンポーネントCSS全体）
  - `portal_tool/docs/access_analytics_billing_cards_design.md`（「1. 画面設計」「2. ルーティング
    設計」節。フォーム項目名・エンドポイント一覧の一次情報）

## 6. Claude Design への期待成果物

次のいずれか（または両方）の形式で提案してください。

1. 画面ごとのデザイン説明: レイアウト・視覚的階層・状態遷移（進捗中/完了/エラー、final/
   provisional/unknown 等）の言語化。2.3節の観点への回答を含める。
2. 具体案: 既存CSSクラスを使った `access_analytics.html` / `billing.html` のJinja2テンプレート案
   のHTML、または既存CSSクラス名を使ったコンポーネント配置図（テキストでの配置説明でも可）。

本プロンプト自体は、最終的な見た目の決定を先取りしません。3節・5節で示したのはデザイントークン・
既存コンポーネント・技術制約の引用であり、レイアウトの組み方・階層表現・文言・折りたたみの範囲
などの具体的なデザイン判断はClaude Design に委ねます。

## 7. スコープ外（この依頼の対象外）

- 楽天BillPay自動化ロジック・Playwright実装・サービス層コードの設計（詳細設計書「3. サービス層
  設計」以降で別途定義済み）。
- Shopify向け画面、広告データ（RPP・アイテムリーチ広告）関連の画面。
- `dashboard.html` への `tool-card` 追加実装、既存4画面のUI変更。
- 分析用ダッシュボード（BIツール等での可視化）。本依頼は取得画面のUIのみを対象とする。
