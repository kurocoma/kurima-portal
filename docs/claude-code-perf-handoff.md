# 引き継ぎ: くりまポータル パフォーマンス改善（Part B 遅延ロードの続き）

作成日: 2026-06-30

## 対象
`C:\Users\hppym\dev\pad-python\portal_tool`（FastAPI/Jinja、git dirty）。日本語で対応。
venv: `C:\Users\hppym\dev\pad-python\portal_tool\.venv\Scripts\python.exe`

## 経緯
UIリニューアル後「トップ→下層ページのリンクが遅い」問題に対応中。
原因特定済み: 商品マスタ `商品管理シート.xlsm`(3MB) を各ページGET毎に openpyxl でフルパース
→ `/yamato` 2.5秒・`/inventory` 1.2秒・`/clickpost` 2.7秒（TTFB=サーバー内部処理時間）。
ユーザー方針: **「キャッシュ＋遅延ロード」の両方**を実装する。

## 完了済み（変更しない・検証済み）

### UIリニューアル（4画面）
`styles.css` 刷新, `base.html`/`dashboard.html`/`yamato.html`/`inventory.html`/`clickpost.html`, `logo.png`。

### Part A: マスタ読み込みキャッシュ（効果実証済み: 2回目以降 0.02秒, 110〜150倍速）
- 新規 `portal_app/services/master_cache.py` … `cached_by_mtime(path, key, loader)`（mtimeベースのメモ化）
- `yamato_conversion.py`: `read_excel_table` をキャッシュ化（重い処理は `_read_excel_table_impl` に分離）
- `inventory.py`: `read_master_tables` をキャッシュ化（DataFrameは `.copy()` 返し、`_read_master_tables_impl`）
- `clickpost.py`: `_load_master_content_rules` をキャッシュ化（`_read_master_content_rules_impl`）
- 検証: ポート8011（--reload）で /yamato 2.46→0.022s, /inventory 1.23→0.020s, /clickpost 2.67→0.017s

## 進行中: Part B 遅延ロード（yamato から実装、未完）

狙い: GETで重いプレビューをスキップし操作パネルを即表示。プレビューは非同期で別エンドポイントから読み込む。
これで「初回（キャッシュmiss）」の体感も改善する。

設計:
- `GET /yamato` → preview を呼ばず操作パネル＋プレースホルダ（`defer_preview=True`）
- 新規 `GET /yamato/preview` → preview実行し結果fragment（`_yamato_results.html`）を返す
- POST系（run-selected等）は従来通り結果インライン（`defer_preview` 無し=False）
- `base.html` の共通JSが `[data-lazy]` を fetch → `outerHTML` を置換

### yamato 残作業（ここから）

> 直前のセッションで `_yamato_results.html` の作成はツール形式エラーで**失敗しており、ファイルは未作成**。
> `yamato.html` はまだ結果セクション（110〜336行）を含んだ状態。ここから再開する。

1. **`_yamato_results.html` を新規作成**。`yamato.html` の結果セクション
   （110行目 `{% if message or result or prepare_result or b2_import_result or restore_result %}`
   〜 対応する 336行目 `{% endif %}`）をそのまま切り出す（base継承なしの純粋fragment、先頭にコメント可）。
2. **`yamato.html`**: 上記 110〜336行を次に置換（337行目 `{% endblock %}` は残す）:
   ```jinja
   {% if defer_preview %}
     <div class="lazy-slot" data-lazy="/yamato/preview">
       <span class="lazy-spinner" aria-hidden="true"></span>最新の変換データを読み込み中…
     </div>
   {% else %}
     {% include "_yamato_results.html" %}
   {% endif %}
   ```
3. **`main.py`**（必ず最新Read。約455行 `yamato_delivery` / 約464行 `_yamato_response`）:
   - `yamato_delivery`: `preview_ne_to_yamato_conversion` を呼ばず `_yamato_response(request, result=None, defer_preview=True)`
   - `_yamato_response`: 引数に `defer_preview: bool = False` を追加し、コンテキストに `"defer_preview": defer_preview` を追加
   - 新規 `GET /yamato/preview`: preview実行 → `templates.TemplateResponse("_yamato_results.html", {request, result, message/error/prepare_result 等})`。例外時は error 付きで同fragment
4. **`base.html`**: `</main>` の後に遅延ロードJSを追加（`[data-lazy]` を fetch して `outerHTML` 置換、失敗時は note error 表示）:
   ```html
   <script>
   document.querySelectorAll('[data-lazy]').forEach(async (el) => {
     try {
       const r = await fetch(el.dataset.lazy);
       el.outerHTML = r.ok ? await r.text()
         : '<div class="note error"><p>読み込みに失敗しました。再読込してください。</p></div>';
     } catch (e) {
       el.innerHTML = '<div class="note error"><p>読み込みに失敗しました。</p></div>';
     }
   });
   </script>
   ```
5. **`styles.css`**（最新Read。ユーザーが `.tabbar` 等を追加済み）: `.lazy-slot` / `.lazy-spinner`（中央寄せ＋回転アニメ）を追記
6. **検証**: ポート8011（--reload）で `/yamato` がHTTP200・操作パネル即表示・プレビューが裏で表示。フォーム契約維持を確認。

### yamato 完了後
`clickpost` も同様に Part B 実装（`_clickpost_results.html` 切り出し＋ `GET /clickpost/preview`）。
※ clickpost は `data-progress-form` / progress-panel の id群・JS を壊さないこと。

## 重要な制約・注意

- **ユーザーが並行編集中＝Write全置換禁止・最新Read後にEdit部分編集**:
  - `inventory.html`（タブUI normal/takaesu）
  - `styles.css`（`.tabbar` 等, `--surface-soft` 変数）
  - `main.py`（`_inventory_tab` / `_inventory_response` / `/inventory/takaesu/prepare`）
- **inventory の Part B は保留**（ユーザーがタブUI開発中。yamato/clickpost を先行）
- **フォーム契約厳守**（壊さない）:
  - `/yamato/run-selected`・`/yamato/restore-print-wait` の input name / 既定値
    （headed=on, import_mode=execute, slow_mo_ms=150, preview_limit=30, 3チェック全ON）
  - clickpost の `data-progress-form` / progress-panel id群（progress-panel/title/message/status/steps/result/error）
  - inventory のダウンロードリンク
- **触らない**: services の業務ロジック本体, `.env`, `data`, `logs`, 外部操作ロジック
- **サーバー**:
  - 本番(ユーザー)=ポート8006・**--reloadなし**（ユーザーが自分で再起動して Part A を反映）
  - 検証用=ポート8011・--reload付き。停止していれば:
    ```
    .venv\Scripts\python.exe -m uvicorn portal_app.main:app --host 127.0.0.1 --port 8011 --reload --app-dir <portal_tool>
    ```
- 検証では**外部操作ボタン（ヤマトB2取込・クリックポスト決済）は押さない**。表示/DOM検証のみ。
- ブラウザ検証で `screenshot`(CDP) がタイムアウトしたら `navigate` でリロードすると復帰。`read_page` は使える。
- Jinja構文チェック: テンプレートを `jinja2.Environment(FileSystemLoader(...))` で `get_template()` するとコンパイルエラーを検出できる。
- 応答時間計測: `curl -w "ttfb=%{time_starttransfer}s total=%{time_total}s" -o /dev/null -s <URL>`

## 参考資料
- handoff: `docs/claude-code-kurima-portal-handoff.md`, `portal_tool/docs/claude-code-ui-redesign-brief.md`
- デザイン案: `docs/くりまのツールポータル設計-handoff.zip` 内 `untitled/project/くりまポータル改善案.dc.html`
