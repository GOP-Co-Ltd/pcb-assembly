# MR4 orchestrator ノート

## 計画

`memory/agents/implementation-planner/ml-core-4-training-experiment.md` を正典とする。
承認済み上位計画（`plans/mr185-...-modular-sonnet.md`）との差分は計画書の「設計判断」表に
根拠つきで記載済み。

## 確認事項への裁定（orchestrator、2026-09-04）

### 1. `OptimizerGroupResult.outcome` に `"gradient_overflow"` を追加する — 承認

上位計画の 4 値は骨子であって網羅ではない。GradScaler の step skip は AMP の**正常動作**であり、
これを `committed` に混ぜると `global_step` が実際の更新回数と食い違い、
run 全体を FAILED にすると正常動作を障害として扱うことになる。どちらも記録の正確さを損なう。

group 先頭の RNG snapshot へ巻き戻して同じ group を再試行する設計も正しい。
`scaler.update()` が scale を下げた後に同じ入力でやり直すのが GradScaler の想定運用だから。
連続上限 8 回は module 定数として定義し、超えたら `non_finite` 扱いにする。

### 2. MLflow の実 local server テストを CI で常時走らせる — 承認（skip 条件を絞る）

上位計画の「MLflow は 3rd-party 表面なのでモックしない」を維持する。CI は `--all-groups` なので
`ml-train` が入り mlflow は必ず存在する。したがって:

- **mlflow が import できないことを理由に skip してはいけない**（CI では常に import できる）
- skip してよいのは「server が port を bind できない」等の実環境失敗だけ。
    その場合も skip 理由に何が失敗したかを書き、静かに緑にしない
- session fixture で 1 度だけ起動する。起動が CI の per-test timeout 180 秒を圧迫するようなら
    `sqlite:///` の file store に落とす（MLflow の実表面であることは変わらないのでモック化ではない）

### 3. `GaussianRegressionTask` を具象クラスにする — 承認

AGENTS.md 開発原則 2「要求されていない抽象化を追加しない」。`GaussianBatch` 値オブジェクトを
受ける具象で足り、ドメインは `TrainingData.materialize` で batch を作るだけでよい。
ABC は上位計画どおり 3 つ（`ExperimentLogger` / `TrainingTask` / `TrainingData`）のまま。

## `state_dict()` の private 名問題

MR4 で扱う（計画書の方針を承認）。checkpoint が初めて存在するのが MR4 であり、
MR5 送りにすると既存 checkpoint への retrofit になるため。
キー正規化 map と `_padding_pixel` の public 化はいずれも却下（計画書に理由記載）。
