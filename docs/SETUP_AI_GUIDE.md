# 新PCへの導入手順書（AIアシスタント向け）

この文書は、新しい Windows PC へ「くりまポータルツール」を導入する作業を
AIアシスタント（Claude Code 等）に依頼するためのものです。
この文書ファイル（または内容のコピー）をそのままAIに渡してください。

---

## AIへの指示（この文書の使い方）

- ステップ順に進めてください。各ステップの **確認** コマンドが通ってから次へ進むこと。
- 失敗したら各ステップの **うまくいかないとき** に従うこと。同じ対処を3回試して
  解決しない場合は、状況をまとめて依頼者に報告して指示を待つこと。
- **絶対に守ること（安全上の禁止事項）**:
  - 導入確認では画面が開くことの確認まで。**「決済」「B2取込」「まとめ実行」等の
    業務実行ボタンは押さない**（実際の支払い・出荷データ変更が発生します）。
  - このアプリを公開インターネットに公開しない。ルーターのポート開放・トンネル
    サービス（ngrok等）の使用も禁止。社内LAN限定。
  - `.env` や共有設定の中身（パスワード・セキュリティコード）をチャット出力に貼らない。

## 0. 前提（依頼者=人間がやっておくこと）

- Windows 10/11 の PC で、管理者権限のあるアカウントでログインしていること
- **OneDrive で SharePoint「くりまポータル」ライブラリが同期済み**であること
  （エクスプローラーで `C:\Users\<ユーザー名>\株式会社しまのや\くりまポータル - ドキュメント` が開ける）
- GitHub リポジトリ（ https://github.com/kurocoma/kurima-portal ）へのアクセスは
  依頼者が用意します（private の場合の認証も依頼者が実施）

## 1. 現状把握（AIが最初に実行）

以下を実行し、何が有って何が無いかを把握する:

```bat
echo %USERPROFILE%
dir "%USERPROFILE%\株式会社しまのや\くりまポータル - ドキュメント" | findstr /i "商品管理シート"
git --version
uv --version
winget --version
dir "%USERPROFILE%\kurima-portal" 2>nul
```

- OneDrive フォルダが開けない場合 → ここで停止し、依頼者に OneDrive 同期を依頼
  （AIでは対処できない。以降の手順はすべてこのフォルダが前提）。
- **Python のインストールは不要**。後述の uv が必要な Python（3.11+）を自動取得する。
  既存の Python が入っていても使わないので、バージョンが古くても問題ない。

## 2. ツール導入（無いものだけ）

### git が無い場合

```bat
winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
```

- **うまくいかないとき**（winget が無い/会社ポリシーで失敗）:
  https://git-scm.com/download/win のインストーラをダウンロードして既定設定で実行。
  それも制限される場合は依頼者（管理者）に報告。
- 導入後は**新しいターミナルを開き直して** `git --version` を確認。

### uv が無い場合

```bat
winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
```

- **うまくいかないとき**: 公式スクリプトで導入:
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  ```
  その後 PATH に `%USERPROFILE%\.local\bin` を追加（新しいターミナルで `uv --version` 確認）。

## 3. リポジトリ取得

手順1で `%USERPROFILE%\kurima-portal` が既に存在すればスキップ。

```bat
git clone https://github.com/kurocoma/kurima-portal.git "%USERPROFILE%\kurima-portal"
```

- 認証を求められて進めない場合は依頼者に依頼（依頼者がGitHub接続を用意する取り決め）。

## 4. セットアップ（自動）

```bat
cd /d "%USERPROFILE%\kurima-portal"
setup.bat
```

`setup.bat` が uv sync（依存＋Python本体）→ Playwright ブラウザ（chromium）→
`.env` 作成 → 環境診断（doctor）まで自動で行う。

- **うまくいかないとき**（setup.bat が途中で失敗）は手動で同じことを実行:
  ```bat
  uv sync
  uv run playwright install chromium
  copy .env.example .env
  uv run python scripts/doctor.py
  ```

## 5. 環境変数（通常は編集不要）

会社共通の認証情報などは **共有設定**
（`くりまポータル - ドキュメント\kurimaportal-app\.env`）から自動で読み込まれるため、
**新PCの `.env` に書き込む値は原則ない**。読み込み優先順位:

1. OS の環境変数 → 2. `.env.local`（端末固有） → 3. 共有 `.env`（kurimaportal-app） → 4. ローカル `.env`

- 手順6の診断で「未設定」と出た認証キーが共有 `.env` に有るはずなのに読めていない場合は、
  OneDrive の `kurimaportal-app` フォルダが同期されているかを確認する。
- この PC だけ設定を変えたい場合のみ `.env.local` に書く（例: `KURIMA_PORT=8010`）。

## 6. 環境診断（全て ○ になるまで）

```bat
uv run python scripts/doctor.py
```

Python / uv / Playwright ブラウザ / ポータル同期フォルダ / 認証系環境変数を ○× で検査する
（終了コード = 失敗数）。× の項目には対処コマンドが表示されるので、それを実行して再診断。
全て ○ になるまで次へ進まない。

## 7. 起動と動作確認

```powershell
cd $env:USERPROFILE\kurima-portal
.\scripts\serve.ps1
```

ブラウザ（または curl）で確認:

```bat
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8006/health
```

- `/health` が 200
- ブラウザで http://127.0.0.1:8006/ を開き、トップに「受注明細データの鮮度
  （最新CSVのファイル名）」が表示される（= OneDrive パス解決が正常）
- ナビから「在庫明細確認」「ヤマト伝票」「クリックポスト」「出荷確定」の各ページが開く
  （**ページが開くことの確認のみ。実行ボタンは押さない**）

- **うまくいかないとき**:
  - ポート使用中 → `scripts\restart.bat`（既存プロセスを止めて起動し直す）、
    または `.env.local` に `KURIMA_PORT=8010` を書いてポート変更
  - トップにパスのエラーが出る → OneDrive 同期を確認（手順1参照）
  - 「商品管理シート…を読み取れませんでした」 → 誰かが Excel でマスタを開いている。
    閉じてもらってページを再読込（アプリ側にリトライあり。恒常的なら依頼者へ報告）

## 8. 運用形態の確認（依頼者に選んでもらう）

| 形態 | 内容 | 追加作業 |
|---|---|---|
| A. LAN共有 | ホスト1台で起動し、この PC はブラウザで `http://<ホスト名>:8006/` を開くだけ | このPCへの導入自体が不要な場合もある。依頼者に確認 |
| B. このPCでローカル起動 | このPCで serve.ps1 を起動して使う（テスト運用と同じ形） | 必要ならスタートアップ登録: `scripts\register_autostart.bat` |

ホストPCとして常駐させる場合のみ（依頼者の指示があった場合）:

```bat
scripts\register_autostart.bat /watchdog   … ログオン時自動起動＋5分毎の死活監視
```

LAN 公開はホストPCで `.\scripts\serve.ps1 -Mode lan` ＋ ファイアウォール受信許可
（詳細は README.md「LAN 共有」節）。

## 9. 導入後の更新方法（依頼者に伝える）

コード更新が配られたら、リポジトリ直下の **`update.bat` をダブルクリック**
（git pull → uv sync → 再起動確認まで自動）。
再起動しないと旧コードのまま動く点に注意（画面フッターの「稼働版」で確認可能）。

## 10. 完了報告（AIが最後に埋めて報告する）

```
■ くりまポータル導入報告
- PC名 / ユーザー名:
- doctor.py: 全○ / 残×あり（項目:      ）
- /health: 200 / NG
- 各ページ表示: OK / NG（ページ:      ）
- トップの受注データ鮮度表示: OK / NG
- 運用形態: A(ブラウザのみ) / B(ローカル起動) / ホスト常駐
- 追加で行った対処:
- 依頼者に確認してほしい残件:
```
