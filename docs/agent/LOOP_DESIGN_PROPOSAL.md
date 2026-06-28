# Eval Loop Design Proposal

## 前提

対象は `portal_tool` の Yamato B2 CSV 変換です。Power Automate Desktop と Excel / Power Query の参照実装から、Next Engine ヤマト配送CSVを Yamato B2 取込用CSVへ変換する処理をPythonへ移行します。

通常loopは固定入力CSVから `ne-to-yamato*.csv` 相当の成果物を生成し、参照CSVと機械比較します。Next Engine実ダウンロード、納品書PDFダウンロード、ステータス復旧、Yamato B2実アップロードは外部状態が変わるため、通常loopとは別のintegration gateにします。

## 案A

- generator: 新規Custom Subagent
- evaluator: 新規read-only Custom Subagent
- Stop hook: state判定

| 観点 | 評価 |
|---|---|
| コンテキスト隔離の強さ | 中。新規sub-agentは使えるが、read-only強制や過去情報遮断はツール面に依存する |
| evaluatorが過去情報を見る可能性 | 低から中。渡す入力を制限できるが、sub-agent内部の権限制御は完全ではない |
| 実装難易度 | 低 |
| 安定性 | 中。前回試行では成果物が共有workspaceへ確実に残らないケースがあった |
| コスト | 中 |
| デバッグ容易性 | 高 |
| 現在のCodex環境での対応状況 | multi-agentはあるが、今回の永続ファイル保存には追加確認が必要 |
| 長時間運用への適合性 | 中。schema固定とread-only強制が弱くなりやすい |

## 案B

- generator: `codex exec --ephemeral --sandbox workspace-write`
- evaluator: 一時evaluation package上の `codex exec --ephemeral`
- evaluator sandbox: read-only
- evaluator output: JSON Schema固定
- Stop hook: state判定

| 観点 | 評価 |
|---|---|
| コンテキスト隔離の強さ | 高。評価packageだけを渡し、過去会話、過去eval、scoreを含めない |
| evaluatorが過去情報を見る可能性 | 低。packageに過去eval/score/plan/reportを入れない限り見えない |
| 実装難易度 | 中。package作成、schema、出力保存が必要 |
| 安定性 | 高。ローカルプロセス実行なので保存物と終了コードを検査できる |
| コスト | 中から高。evaluatorごとに別実行が必要 |
| デバッグ容易性 | 中。eval packageとJSON出力を保存すれば再現できる |
| 現在のCodex環境での対応状況 | `codex exec --ephemeral`、`--sandbox`、`--output-schema` が利用可能 |
| 長時間運用への適合性 | 高。context rotと採点ドリフトに強い |

## 採用案

案Bを採用します。generator/evaluatorはいずれも runner script から毎周 `codex exec --ephemeral` で起動します。`scripts/run_loop_generator.py` は許可ファイルだけを一時workspaceへコピーし、`scripts/run_loop_evaluator.py` は一時evaluation packageを作ってそこを作業ディレクトリにします。evaluatorには評価packageだけを渡し、read-only sandbox と JSON Schema 出力で採点契約を固定します。`/fork` は現在会話の継承可能性があるため採用しません。

## 役割別の実装

### Orchestrator

メインCodex。ループ開始後は成果物の実装と採点を行いません。書いてよいものは plan、state更新入力、progress、decisions、traceability、context manifest に限定します。

### Generator

`.codex/agents/loop-generator.toml` で契約を固定します。毎周 `scripts/run_loop_generator.py` が作る sanitized workspace から fresh `codex exec --ephemeral --sandbox workspace-write` として起動し、元依頼、criteria、turn plan、対象コード、必要テストだけを読みます。過去eval、score、best score、採点会話は渡しません。

### Evaluator

`.codex/agents/loop-evaluator.toml` は契約文書として保存します。実行時は `scripts/run_loop_evaluator.py` が一時evaluation packageを作り、task、criteria、schema、成果物、参照成果物、検証reportだけを渡します。plan、generator report、state、過去scoreは渡しません。

### Progress Controller

`.codex/hooks/stop_eval_loop.py`。AI Agentではありません。`.loop/current/state.json` だけを読み、active、score、threshold、hard gate、consecutive、iteration、blocked_reason だけで継続/終了を決めます。

## 初期Hard Gate

Yamato B2 CSV変換のPower Automate/Excel同等性:

- 固定入力: `C:\Users\hppym\株式会社しまのや\くりまポータル - ドキュメント\ネクストエンジン\ne-yamatocsv\ne-yamato2606251604.csv`
- 参照成果物: `C:\Users\hppym\株式会社しまのや\くりまポータル - ドキュメント\ネクストエンジン\完成データ\ne-to-yamato2606260043.csv`
- 実行: `scripts/validate_equivalence.py --config validation/equivalence.yaml`
- 出力: `validation/runs/yamato-b2-equivalence-<run-id>/generated-ne-to-yamato.csv` と `report.json`
- 比較: byte exact、CP932読込、CRLF、ヘッダー、行数、列数、42列

## 通常loopに含めないもの

- Next Engine からの実ダウンロード
- 納品書PDFダウンロード
- 受注ステータス復旧
- Yamato B2実アップロード

これらは外部状態を変えるため、通常loopで5回連続検証の対象にはしません。
