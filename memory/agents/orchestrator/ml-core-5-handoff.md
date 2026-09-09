# MR5（設定合成 + ハイパーパラメータ探索）引き継ぎ

別マシンで MR5 を進めるための資料。2026-09-07 時点。

コア ML 基盤 6 本の 5 本目にあたる。MR1〜MR4 は `main` に merge 済みで、MR5 は
`main` から分岐して進める（stacked にする必要はもう無い）。

## 1. 現在地

`main` = `edce7e6`。

| MR | 内容 | 状態 |
| --- | --- | --- |
| !195 | pcbasm の validate メソッド化 | merged |
| !196 | `ml.artifact` / `ml.serialization`、依存グループ、CI 配線 | merged |
| !197 | `ml.data`（前処理 / batch / split） | merged |
| !198 | `ml.model` / `ml.evaluation` | merged |
| !199 | ml の flat 関数 23 個を classmethod へ | merged |
| !200 / !201 | pcbasm / web の同種リファクタ | merged |
| !202 | **MR4** training core + experiment tracking | merged |
| — | **MR5** 設定合成 + HPO | **これから** |
| — | MR6 export（ONNX / parity / INT8 / package） | 未着手 |

`src/ml/` の現状（tracked なもの）:

```
src/ml/__init__.py          docstring のみ。re-export しない
src/ml/serialization.py     strict cattrs converter（make_strict_converter / structure_strictly）
src/ml/artifact/            atomic.py fingerprint.py document.py package.py
src/ml/data/                image.py batch.py split.py
src/ml/model/               blocks.py heads.py loss.py inspection.py
src/ml/evaluation/          regression.py slices.py compile_parity.py _aggregation.py
src/ml/experiment/          logger.py mlflow.py provenance.py
src/ml/training/            task.py data.py transaction.py checkpoint.py random_state.py loop.py
```

`src/ml/config/` は `__pycache__` だけの未追跡ディレクトリで、中身は無い。当初計画の
`ml/config/converter.py` は依存の向きの都合で `src/ml/serialization.py` へ移した
（`ml.artifact.document` が `ml.config` を import するのは逆向きだった）。MR5 で
`ml/config/` を作るなら、この経緯を踏まえて「設定フレームワークの境界」だけを置く。

## 2. リポジトリ外にある資料（持ち出しが必要）

**承認済みのマスタープランは git 管理外**にある。

```
/home/gop/.claude/plans/mr185-codex-docs-image-based-dispense-ca-modular-sonnet.md
```

MR5 に必要な内容は本資料の §3〜§7 に取り込んであるので、コピーできないなら本資料だけで進めてよい。
リポジトリ内にある関連メモは以下（すべて `main` に入っている）。

- `memory/agents/implementation-planner/ml-core-4-training-experiment.md` — MR4 の計画（791 行）
- `memory/agents/orchestrator/ml-core-4-training-experiment.md` — MR4 の裁定（1 巡目・2 巡目）
- `memory/agents/code-reviewer/ml-core-4-training-experiment.md` — MR4 のレビュー全 3 巡
- `memory/agents/spec-test-author/ml-core-4-training-experiment.md` — テスト観点の対応表
- `AGENTS.md` / `CLAUDE.md` — 常時参照する規約

## 3. MR5 で最初に決めること：Hydra を使うか

**これが MR5 の本題。** ユーザーの指示は次のとおり。

> hydra を使うことは必須ではなく、並列 run や config の書き換えが効率的にできればそれでよい。
> ただしその選択として hydra が最も有効な場合は、dev 版を許す。

つまり**要件は「並列 run」と「config 上書き」であって、Hydra は手段の候補にすぎない**。

| 案 | 内容 |
| --- | --- |
| A | Hydra + stock `hydra-optuna-sweeper`。`1.4.0.dev9` に固定される |
| B | Hydra（設定合成と multirun のみ）+ Optuna を自前の薄い runner で直接駆動。dev release 依存が消える |
| C | Hydra なし。attrs config + YAML / CLI 上書きを自前で書き、Optuna を直接駆動 |

### 判断に必要な既知の事実

`pyproject.toml` の `[dependency-groups]` は既にこうなっている。

```toml
ml-train = [
    { include-group = "ml-runtime" },
    "hydra-core>=1.3,<2",
    "mlflow>=3.15,<4",
]
ml-hpo = [
    { include-group = "ml-train" },
    # optuna 4.x に対応する hydra-optuna-sweeper は dev release しか存在しない
    # （最新安定版 1.2.0 は optuna<3.0.0 を要求する）。
    "hydra-optuna-sweeper==1.4.0.dev9",
    "optuna>=4.9,<5",
]
```

案 B / C を採るなら `hydra-optuna-sweeper` の行を削る。案 C なら `hydra-core` も削る。
CI は `uv sync --locked --all-groups` なので、依存を変えたら `uv.lock` の更新が要る。

**一般論で決めないこと。** 「Hydra は業界標準だから」ではなく、
案 C で自作することになる量（`OmegaConf` 相当の合成、CLI 上書き、multirun の並列実行）を
実際に見積もったうえで決める。推奨案と、採らなかった案を採るべき条件を書き残す。

### MR185 の失敗から避けること

MR185 は `hydra_plugins.hydra_optuna_sweeper._impl.OptunaSweeperImpl` を継承して
private メソッドを override し、4 階層の委譲ラッパーを重ねていた。**これは再現しない。**
カスタムが必要だった 4 点の代替は次のとおり。

| MR185 のカスタム | 代替 |
| --- | --- |
| 決定論的 study 名 | OmegaConf custom resolver（`${ml.study_name:...}`）を登録して YAML 側で解決 |
| trial ごとの checkpoint dir / run 名の注入 | 同じく resolver（`${hydra:job.num}`）で YAML に書く |
| resume 時の `n_trials` 減算 | Optuna の `load_if_exists` に任せる。追加 trial として積む方が意味が明確 |
| MLflow lineage の強制 | sweep 内ではなく事後の純関数 `ml.tuning.study.verify_study_lineage()` で検証 |

## 4. MR5 のスコープ

- 設定合成の境界（案 A / B なら `ml/config/hydra.py`）。
    `DictConfig → plain dict → strict cattrs → frozen attrs` の一方向変換。
    `HydraConfig.get().runtime.cwd` 基準で相対パスを絶対化する。custom resolver の登録
- packaged config group（wheel 同梱。`[tool.uv.build-backend] module-name` に `ml` が
    入っているので、YAML が wheel に含まれることを確認する）
- `ml/tuning/study.py` — study 名生成 / storage 検証 / credential 秘匿 /
    `optimization_results.yaml` / lineage 検証。**すべて純関数**
- **既定値の二重管理を避ける。** 既定値は attrs config クラスにのみ置き、YAML は差分だけ書く。
    `YAML のキー ⊆ attrs のフィールド` をテストで固定する

どの案を採っても、**学習 core は設定フレームワークを import しない**という構造は変えない。
境界は `ml/serialization.py` の strict converter に置く。

## 5. MR4 から引き継ぐ制約

MR5 の設計を縛るもの。詳細は `memory/agents/orchestrator/ml-core-4-training-experiment.md`。

- **`TrainerConfig.fingerprint` は時間予算 4 フィールドを除外している**
    （`deadline_seconds` / `finalization_grace_seconds` / `checkpoint_interval_steps` /
    `checkpoint_interval_seconds`）。含めると deadline で中断した run を新しい予算で
    resume できなくなるため。config 合成側がこの区別を壊さないこと。
    分類は `tests/ml/training/test_loop.py` が pin していて、新フィールドは
    どちらかへの分類を強制される
- **時間予算は MLflow の param ではなく tag（`training.` 前置き）として記録する。**
    param は一度記録すると値を変えられず、resume で値が変わると MLflow が拒否する。
    **探索で変わる値を param 側へ入れない設計にすること**
- `TrainingData.materialize` は `device` を受け取り、tensor をその device 上に作る契約
- `TrainingTask.model` は compile 後も compile 前の `nn.Module` を返す契約。
    `state_dict` のキー集合は公開契約として literal で pin 済み
- ABC は 3 つだけ（`ExperimentLogger` / `TrainingTask` / `TrainingData`）。増やさない

### state_dict の private 名問題は MR4 で決着済み

`_encoder._padding_pixel` のようなキーは正規化せず、**契約として固定し破ったら落ちる**方式を採った。
キー列を `tests/ml/training/test_checkpoint.py` で literal に pin し、
`TrainingCheckpoint.resume_rejection()` が不一致を `str | None` で拒否する。
キー正規化 map（リネームが不可視になる）と `_padding_pixel` の public 化
（カプセル化規約違反）はいずれも却下した。MR5 で蒸し返さないこと。

## 6. 守る規約

`AGENTS.md` と `CLAUDE.md` が正典。ML 基盤で特に効いているものだけ再掲する。

- **`src/ml/` は `pcbasm` / `web` を絶対に import しない。**
    `tests/ml/test_architecture.py` が AST で機械検証する（相対 import も検出）
- **層ごとの依存を守る。** `ml.config.hydra` は `ml-train` 層、`ml.tuning` は `ml-hpo` 層。
    `ml-runtime` だけの Raspberry Pi 5 で推論経路が import できることを壊さない
    （これも `test_architecture.py` が subprocess で検証している）
- **ABC + `@override`。Protocol は使わない。**
    pyright の `reportImplicitOverride = true` が Protocol 実装には効かないため
- **`@attrs.frozen`。** `@dataclass` は使わない
- **検証は例外を投げず `validate() -> str | None` メソッド**として所有クラスに持たせる
- **クラスに属する関数は module-level に置かず classmethod にする。**
    既存の型: `ModelSize.measure` / `SplitManifest.build` / `PaddedBatch.pad`
- **略語を使わず綴りきる。** `hyperparameter_search` / `learning_rate` / `configuration` /
    `optimizer` / `standard_deviation`。codespell の ignore-list で誤検知を抑えない
- **docstring・エラーメッセージは日本語**

## 7. 検証と禁止事項

```bash
make format          # 2 回続けて走らせる
make type
make test-no-hardware
```

**`make test` / `make run` / `pytest -m hardware` は絶対に実行しない。**
実機（カメラ、Klipper のステージ / サーボ / エアポンプ、GPIO）が物理的に動く。
`.claude/settings.json` の deny と PreToolUse hook で機構的に禁止済み。実機確認はユーザーが行う。
pytest を直接叩くときは**対象パスに関わらず必ず `-m "not hardware"` を付ける**。

MR4 merge 時点の基準値: `make type` 0 errors、`make test-no-hardware` **3350 passed**。

## 8. 運用上の落とし穴（このセッションで踏んだもの）

**`make format` は未追跡ファイルを検査しない。** pre-commit は git が知っているファイルだけを
対象にするので、新規作成してまだ `git add` していないファイルは `make format` が
2 回連続で pass しても一度も検査されていない。MR4 で新規 22 ファイルが全て未検査のまま
「format 通過」と報告され、`git add` した瞬間に docformatter が日本語 docstring を壊した。
**新規ファイルを含む変更では `git add -A` してから `make format` を走らせる。**

**docformatter が日本語の複数文段落を壊す。** `--wrap-descriptions=72` は文字数で数えるので、
1 文を 2 行に折り返して書くと句読点の直後を半角空白で継いで
「落ち、 resume 拒否が」のような壊れた文になる。
**docstring の説明部は 1 文 1 段落**（1 文ごとに空行で区切る）。
summary を小文字の識別子で始めない（先頭が大文字化される）。

**変異実験の後始末を必ず確認する。** MR4 のレビューでは「機構を潰してもテストが緑」という
指摘が 2 巡続き、変異実験が有効だった。ただし**エージェントが途中で kill されると
変異が戻らない**。このセッションでは `src/ml/training/loop.py` の `try/except` が
消えたまま残っていた。変異実験のあとは必ず `git status` と `git diff` を見る。

**stacked MR は target を付け替えると `head_pipeline` が消える。**
`detailed_merge_status: ci_must_pass` で止まるので、
`glab api projects/:id/merge_requests/<iid>/pipelines -X POST` で
merge_request pipeline を作ってから `glab mr merge --auto-merge` する。
MR5 は `main` から分岐すればこの問題を踏まない。

**worktree で他のエージェントが作業中は `git -C` でリポジトリを明示する。**
Bash の作業ディレクトリは呼び出し間で持ち越されるので、`cd` した次の呼び出しで
`git checkout` すると他人の worktree を切り替えてしまう。

**`glab` の OAuth トークンは失効する。** `invalid_grant` が出たら
`glab auth login --hostname gitlab.com` をユーザーに依頼する（ブラウザが要る）。

**`.claude/settings.json` に一回限りのコマンド許可が紛れ込むことがある。**
コミット前に `git status .claude/` を確認して revert する。

## 9. 進め方

`main` から `feature/<日付>/ml-core-5-config-tuning` を切る。

MR4 では `implementation-planner` → `spec-test-author` ∥ `plan-implementer` →
`code-reviewer` の順で回し、レビュー 3 巡で approve になった。
**`spec-test-author` を挟む価値はあった**（MR3 で挟まずに must-fix を出したのと対照的）。
ただしレビューの中心的な指摘は 2 巡とも「テストが機構を守っていない」だったので、
**テストを書いたら対応する機構を潰して落ちることを実測する**工程を最初から組み込むこと。

なお 2026-09-07 のセッションでは**サブエージェントが完了前に kill される事象が続いた**
（`ml-mr4-reviewer-3` と `ml-mr5-planner` が両方）。別マシンで同じ症状が出るなら、
委譲せず `solo-dev-cycle` で進めた方が確実。

## 10. MR5 / MR6 への申し送り

- `_restore` が途中で失敗すると、部分的に復元された model から `emergency.pt` が書かれる。
    emergency は post-mortem 専用で resume 対象外なので実害は無いが、MR5 で触るなら意識する
- `GitProvenance` の untracked 読み込みは**メモリだけが有界**で、digest を取る I/O は
    上限を超えたファイルも最後まで読む。`diff_fingerprint` を内容変化に追従させるための
    トレードオフとして受け入れている
- `seed_everything` は `torch.use_deterministic_algorithms` をプロセス全体に立てたまま戻さない。
    docstring に明記済み。context manager 化は「現状顕在化していない問題に機構を足す」ため見送った
- `_weighted_percentile` は weight 比が float64 の分解能内であることを前提にしている
    （実 weight の比は高々 1e4）。weight 設計を変える MR では再確認する
- MR6 の Raspberry Pi 5 実測 benchmark は `@mark_hardware` で分離し、ユーザーが実行する
