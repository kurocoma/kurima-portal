# Eval Loop Requirements

この文書は、添付された「コンテキスト分離されたeval-loop」要求を `portal_tool` の Yamato B2 CSV 変換に適用するための要求抽出です。

対象は `portal_app/services/yamato_conversion.py` と `/yamato` 画面です。通常のeval-loopでは副作用のない固定CSV変換だけを hard gate にします。Next Engine の実ダウンロード、納品書PDFダウンロード、ステータス復旧、Yamato B2実アップロードは状態変更を伴うため、通常loopから除外します。

## 要求一覧

| ID | 資料の要求 | 実装上の意味 | Codexでの実現方法 | 検証方法 |
|---|---|---|---|---|
| R001 | 司令塔、作る係、採点する係、進行係を分離する | 役割ごとの責務と入出力を固定する | orchestratorはメインCodex、generator/evaluatorは毎回fresh `codex exec`、progressはPython hook | smoke testで設定とcontext manifestを検証 |
| R002 | 司令塔は実装しない | ループ開始後のメインスレッドは成果物を直接修正しない | plan/state/progress/manifestのみ作成可 | turn planとreportの所有者を確認 |
| R003 | 司令塔は採点しない | score/passedを文章判断で決めない | evaluator JSONを `record_eval.py` が検証してstate反映 | state更新ログとeval schema検証 |
| R004 | 作る係は戦略を変更しない | generatorはturn planの範囲だけ実装する | generator promptにplan遵守を明記 | reportと変更ファイルを確認 |
| R005 | 作る係は前回の採点結果とscoreを読まない | generatorにeval/state scoreを渡さない | generator context manifestに forbidden files を記録 | smoke testでcontextにeval/scoreがないことを確認 |
| R006 | 採点する係は成果物を変更しない | evaluatorはread-onlyで評価だけ行う | evaluation package と read-only `codex exec --ephemeral` を使う | smoke testで契約を確認 |
| R007 | 採点する係は前回のscoreを読まない | evaluatorには過去eval/stateを渡さない | packageにはtask/criteria/schema/artifacts/referenceだけ入れる | evaluator context manifestを検証 |
| R008 | 採点する係はplanへの適合を採点軸にしない | plan通りかではなく元依頼とcriteriaで絶対評価する | evaluator packageにplan/reportを入れない | eval evidenceとcriteria breakdownを確認 |
| R009 | 採点する係は元依頼と固定criteriaで絶対評価する | 毎周同じcriteria keyで採点する | `.loop/current/criteria.yaml` と eval schemaでbreakdown key固定 | `validate_eval_schema.py` |
| R010 | 作る係と採点する係は毎周、新しいコンテキストで起動する | resumeや既存agent再利用は禁止 | runner script経由の `codex exec --ephemeral` を使う | context manifestの `previous_thread_reused=false` |
| R011 | 進行係は成果物を読まない | 続行判定はstateだけ | `.codex/hooks/stop_eval_loop.py` は state.json のみ参照 | smoke testで判定ケースを検査 |
| R012 | 進行係はstateの数値だけで続行を決める | AI判断やfeedback本文を使わない | active/status/score/threshold/consecutive/hard gateのみ判定 | 89点/90点ケースのsmoke test |
| R013 | 状態は会話ではなくファイルに持つ | resume/compaction後も復元可能にする | `.loop/current/state.json`, progress, feedback, decisions | smoke testで復元可能性を確認 |
| R014 | 元依頼を毎周再読込する | 目標ドリフトを防ぐ | `.loop/current/task.md` を各turnで読む | planにtask hashを記録 |
| R015 | 一周ごとにplan、report、evalを保存する | 監査可能にする | `turns/turn-XXX-plan.md`, report, eval JSON | smoke testで存在確認 |
| R016 | 最良版を別に保存する | best_score時点の成果物参照を残す | `.loop/current/snapshots/best-*` | `record_eval.py` が best_ref を更新 |
| R017 | ループ機構自体にsmoke testを用意する | 本番loop開始前に仕組みを検証する | `scripts/smoke_test_eval_loop.py` | `smoke ok` と終了コード0 |
| R018 | `/fork` をfresh contextと誤認しない | 現会話履歴継承の可能性がある手段を使わない | `/fork`ではなく `codex exec --ephemeral` を採用 | DESIGN_PROPOSALに採否を記録 |
| R019 | evaluator出力はschema固定 | 採点形式の揺れを防ぐ | `.loop/current/eval-schema.json` | `validate_eval_schema.py` |
| R020 | hard gateはPower Automate/Excel同等性を機械比較する | LLM感想で合格させない | `scripts/validate_equivalence.py` | report JSON の `passed=true` |
| R021 | 5回連続成功は独立クリーン実行だけ数える | 1回成功では完了にしない | `record_eval.py` がhash変化時に連続成功をリセット | smoke testでhash変更ケースを確認 |
| R022 | Stop hookは二重blockしない | 同じiterationで無限再入しない | `last_stop_blocked_iteration` をstateに保存 | smoke test |
| R023 | 壊れたstateを成功扱いしない | JSON破損時は一度だけblockして修復要求 | hookがparse失敗をblock扱いする | smoke test |
| R024 | メインへ長いログを貼らない | context rotを避ける | ログは `turns/` と `validation/runs/` に保存 | progressに保存パスだけ記録 |
| R025 | Subagentから司令塔へ返すのは短い要約と保存パスだけ | メインスレッドを汚染しない | generator/evaluator promptで制限 | report/evalファイルの存在確認 |
| R026 | evaluatorをgeneratorの子Agentにしない | 採点を実装文脈から分離する | 両方を司令塔が直接起動する | context manifest |
| R027 | evaluatorへgenerator会話を渡さない | 自己申告採点を避ける | report/plan/conversationはevaluation packageへ入れない | smoke test |
| R028 | compaction前にcheckpointを保存する | 会話要約に依存しない | `.codex/hooks/pre_compact_checkpoint.py` | smoke test |
| R029 | resume後はファイルから現在地を復元する | 会話記憶に依存しない | `.codex/hooks/session_context.py` がstate要約を出す | smoke test |
| R030 | Claude Code固有機能とCodex機能を混同しない | 現在使える機能だけ採用する | `codex exec --ephemeral` とローカルスクリプトを使う | DESIGN_PROPOSALに記録 |

## Codex機能調査メモ

- `codex exec --ephemeral`、`--sandbox`、`--output-schema` を fresh context と構造化出力に使う。
- `/fork` は現在の会話履歴継承の可能性があるため、context refresh手段として採用しない。
- hookの自動発火はCodexクライアント設定に依存するため、同じ判定を手動/CIでも実行できるPythonスクリプトとして配置する。
