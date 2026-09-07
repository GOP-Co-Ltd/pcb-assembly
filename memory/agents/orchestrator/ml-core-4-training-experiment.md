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

## レビュー 1 巡目の裁定（orchestrator、2026-09-04）

`memory/agents/code-reviewer/ml-core-4-training-experiment.md` の verdict は request-changes。

### must-fix は 4 件とも受理、should-fix から 2 件を昇格

| # | 裁定 | 補足 |
| --- | --- | --- |
| 1 fingerprint 除外と `log_params` の不整合 | 受理 | 除外 4 フィールドは param ではなく **tag** へ回す（MLflow の tag は可変）。`start()` 後の記録はすべて例外経路の内側へ入れる |
| 2 device seam 不在 | 受理。**MR4 で直す** | `TrainingData.materialize` に `device` を足す。tensor が生まれる場所で device を決めれば余分な host→device コピーが要らず、Trainer は BatchT を知らないままでいられる。`TrainingTask.move_batch` 案は全 task に移送責務を負わせるので採らない |
| 3 readback validator 未到達テスト | 受理 | `validate()` は通るが readback で落ちる状況を作る |
| 4 RNG 復元が非感応 | 受理 | 合成 task に global RNG を消費する項を入れ、`RandomState.restore` を潰すと落ちることを確認してから確定 |
| 5 `sanitize_persisted_uri` の credential 残留 | **must-fix へ昇格** | credential 除去がこの関数の唯一の役目で、それが実測で機能していない。polish ではなく欠陥 |
| 9 `_finalize` が別 run の `best.pt` を読む | **must-fix へ昇格** | 別 run の weights が `final.pt` として出るのは静かな誤りで、被害が大きいわりに修正は run_id 比較 1 つ |

### should-fix はすべて受理

6（`gradient_accumulation=2` を parametrize に追加）、7（zero_grad を守る比較へ）、
8（`best.pt` の中身を読む assertion）、10（untracked 読み込みにサイズ上限）、
12（`set_tags` 失敗でも `_end_run` へ到達させる）、13（中断 epoch で metrics が空）、
14（parametrize を `FINGERPRINT_FIELDS` から導出）。

**11（`use_deterministic_algorithms` のプロセス全体汚染）は docstring への明記のみ。**
context manager 化は現状の suite で顕在化していない問題に機構を足すことになる
（AGENTS.md 原則 2）。`deterministic=True` は学習 run で意図して立てる設定でもある。

### nit の扱い

受理: `> _MAXIMUM_CONSECUTIVE_GRADIENT_OVERFLOWS` の off-by-one（実際は 9 回許している。
定数名と docstring は 8 回のつもり）、`as_params` の `attrs.fields(type(self))`、
`resume_rejection` の恒真 `run_id` 引数を落として loop 側の比較に一本化。

見送り: `train_samples_per_second` の分母、`logger.flush()` の未使用、
`_capture_failure` が設定ミスでも emergency を残す点。いずれも振る舞いは正しく、
変更を要求されていない。

## レビュー 2 巡目の裁定（orchestrator、2026-09-04）

verdict は再び request-changes。受理した 13 件は実装されていたが、2 点で差し戻し。

### must-fix 1: `sanitize_persisted_uri` の回帰 — orchestrator が修正

1 巡目の修正が `f"{scheme}://{authority}{path}"` を無条件に組んでいたため、
authority を持たない URI が壊れていた（`file:./mlruns` → `file://./mlruns` など）。

**成分から復元する方式そのものが誤り**だった。`urlsplit` は `sqlite:///mlruns.db` と
`sqlite:/mlruns.db` を同じ成分へ潰すので、復元すると別の URI になる。
`sqlite:///` は MLflow の標準 tracking URI 形式で、レビュアーの提案（netloc が
非空のときだけ組み立てる）でも潰れたままだった。

netloc に `@` が無ければ組み立て直さず、query と fragment だけを文字列として
落とす形に変更した。credential 除去と authority 表記の保存が両立する。

### must-fix 2: 受理指摘が回帰テストなしで着地 — 受理

レビュアーの変異実験で、device 引き回し / sanitize / `_finalize` の run_id 照合 /
untracked のサイズ上限 / `set_tags` の握り / overflow の off-by-one が
**潰しても緑**だった。1 巡目と同型の穴なので全件テストを足す。

### should-fix の裁定

| # | 裁定 |
| --- | --- |
| 1 `_existing_best_path` が try の外・二重ロード | 受理。`_RunState` 構築後、try の内側で resume のときだけ呼ぶ形へ。あわせて `_restore` も run_id 照合の後・try の内側へ移した（拒否した resume で呼び出し側の model を壊さない） |
| 2 untracked の I/O が青天井 | **docstring への明記のみ**。digest にも上限を置くと `diff_fingerprint` が同一サイズの内容変更を取りこぼす。fingerprint の正確さを優先する |
| 3 logger 例外時のテストが無い | 受理 |
| 4 `RecordingExperimentLogger` が param 不変契約を模していない | 受理。fake が契約を模さないと must-fix 1 の根本は将来も素通りする |
| 5 `finalization.best_checkpoint_ignored` の truncate 漏れ | 受理 |

### nit の裁定

受理: `as_tags` の key へ `training.` 前置き、overflow 境界のテスト、
`compile_options.*` を param テストの対象に、拒否 resume が model を壊さないテスト。

見送り: `_end_run` 自体が投げた場合（`__context__` に残る。握りを足すのは機構過多）、
`except Exception` が `BaseException` を通す点（logger が `BaseException` を投げる
前提は採らない）。
