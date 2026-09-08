# MR5 設定合成 + ハイパーパラメータ探索：案の選定

## 概要

MR5 の本題である「Hydra を使うか」を、コンテナ内での実測に基づいて判定する。
実装計画の全体は次のラウンドで書く。本資料は案 A / B / C の選定材料に限る。

**結論：案 C（Hydra なし）を推奨する。**
決め手は「Hydra の multirun は実際には並列化しない」ことと、
「stock sweeper は安定版 Hydra では動かず、Hydra スタック全体を pre-release
にしないと成立しない」ことの 2 点。要件（並列 run / config 上書き）に対して
Hydra が寄与しないことが実測で確認できた。

---

## 1. `hydra-optuna-sweeper==1.4.0.dev9` の実体確認

### install 自体は通る

`optuna 4.9.0` と共存する。宣言依存は `hydra-core>=1.1.0.dev7` と `optuna<5.0.0,>=4.9.0`。
`pyproject.toml` の `optuna>=4.9,<5` はこれと整合している。

### しかし安定版 Hydra では sweep が動かない（最重要）

**リポジトリの現在の `ml-hpo` 環境（`/opt/venv`、`uv.lock` 由来）で sweep が失敗する。**

```
hydra-core 1.3.6 / omegaconf 2.3.1 / optuna 4.9.0
$ python app.py --multirun
Error in call to target 'hydra_plugins.hydra_optuna_sweeper.optuna_sweeper.OptunaSweeper':
InstantiationException('Cannot instantiate config of type TPESampler.
Top level config must be an OmegaConf DictConfig/ListConfig object, ...')
full_key: hydra.sweeper
```

原因は 2 つ重なっている。

1. dev9 の `OptunaSweeper.__init__` が
   `instantiate(sampler, _execution_whitelist_=("hydra_plugins.hydra_optuna_sweeper.*", "optuna.samplers.*"))`
   を呼ぶ。`_execution_whitelist_` は hydra-core 1.3.6 に存在しない
   （site-packages/hydra 配下を grep して 0 件）。1.4.0.dev9 では
   `hydra/_internal/target_policy.py` / `_instantiate2.py` / `core/plugins.py` に存在する。
2. hydra-core 1.3.6 の `Plugins._instantiate` は `_recursive_` 既定 True で
   sweeper node を instantiate するため、`sampler` が `__init__` 到達前に
   すでに `TPESampler` インスタンスになっている。plugin 側がそれを再度
   `instantiate` して落ちる。

回避策として stock node に `_recursive_: false` を置くのは**拒否される**。

```
ConfigKeyError: Key '_recursive_' not in 'OptunaSweeperConf'
    full_key: hydra.sweeper._recursive_
```

`OptunaSweeperConf` は structured config なので未知キーを受け付けない。
**MR185 が独自の sweeper node（`paste_volume_optuna.yaml` に `_target_` と
`_recursive_: false`）を登録していたのは、この不整合の回避も兼ねていた。**
MR185 のカスタムは全部が過剰だったわけではなく、一部はバージョン不整合への対処だった。

### 動く組み合わせは pre-release 3 点固定

```
hydra-optuna-sweeper==1.4.0.dev9
hydra-core==1.4.0.dev9
omegaconf==2.4.0.dev15      # hydra-core 1.4.0.dev9 が omegaconf>=2.4.0.dev15 を要求
```

3 点を**明示的に固定すれば** uv は global な `--prerelease=allow` なしで解決でき、
`sqlalchemy` は安定版 2.0.52 に留まる。
`--prerelease=allow` で代替すると `sqlalchemy==2.1.0rc1` まで pre-release に流れる。

この 3 点構成では 6 trial の sweep が実際に完走し、`optimization_results.yaml` が出る。
ただし中身は `name` / `best_params` / `best_value` だけで、trial ごとの記録も
lineage も無い（§4 が求める成果物は結局自前で書く必要がある）。

### dev release 固定のリスク

| 観点 | 実測 |
| --- | --- |
| yank されうるか | **履歴上 yank ゼロ**（0.9.0rc1〜1.4.0.dev9 の全 release）。yank リスクは低い |
| churn | dev4〜dev9 が 2026-06-22〜2026-08-30 に約 2 週間おき。dev9 は 1 週間前 |
| hydra-core との結合 | dev9 同士の upload が **3 秒差**（sweeper 07:55:00 / hydra-core 07:54:57）。dev4/5/6/8 も同様。**sweeper devN は hydra-core devN と lockstep** |
| pin 解除の見通し | 安定版 sweeper 1.2.0 は **2022-05-17**（約 4 年前、`optuna<3.0.0`）。1.4.0 final には hydra-core 1.4.0 final が必要で、`hydra-core 1.4.0.dev1` は **2024-07-10**。2 年以上 dev のまま。ETA 不明 |
| API churn の兆候 | 1.4.0.dev9 で `version_base` が `Hydra15MigrationWarning` で非推奨化済み |

つまり「dev 版を 1 つ許す」ではなく「**Hydra スタック 3 点を 2 週間 cadence の
pre-release に載せ、解除時期は不明**」という選択になる。

---

## 2. 案 C の自作量（実測）

`/tmp` に実物を書いて、リポジトリの本物の `ml.serialization` に通して測った。

**`composition.py` = 103 行**（docstring・空行込み）。内容は次の 4 つだけ。

- `load_layer` — YAML を 1 層読む（mapping でなければ理由文字列）
- `merge_layers` / `_merge_into` — 後の層優先の再帰 merge（list は置換）
- `apply_overrides` / `_assign` / `parse_scalar` — `trainer.learning_rate=3e-4` 形式
- `canonical_json` — 記録・fingerprint 用の正規化

本物の `make_strict_converter()` / `structure_strictly()` と attrs frozen config に対して
6 ケースを通した（すべて期待どおり）。

| 検証 | 結果 |
| --- | --- |
| 既定値のみ（YAML も上書きも無し） | attrs の既定値で構造化 |
| YAML 差分 2 層の merge（base + pi） | `{"trainer":{"deadline_seconds":3300.0,"device":"cpu","gradient_accumulation_steps":4,"learning_rate":0.0001,"max_epochs":60}}` → 正しく構造化 |
| CLI dotted 上書き（既存キー・別セクション） | 反映 |
| 未知キー（`trainer.lerning_rate`） | `extra fields found (lerning_rate) @ $.trainer` で拒否 |
| 型違い（float 宣言に int `1`） | `invalid value for type, expected float @ $.trainer.learning_rate` で拒否 |
| 壊れた上書き（`=` なし） | `上書きは key=value 形式で指定してください` |

### §4「既定値は attrs にのみ置き YAML は差分だけ」が削る機能

この方針が、Hydra の合成機能のほとんどを不要にする。

| Hydra 機能 | 本当に必要か | 案 C での代替 |
| --- | --- | --- |
| `defaults` list / config group | **不要**。MR185 の `trainer/gpu.yaml` と `trainer/pi.yaml` は 19 フィールドを両方が全部書き直していた（差があるのは 8 個）。差分だけにすると各 5 行程度になり、「大きなブロックを差し替える」という config group の存在理由が消える | CLI で層を順序付きリストで指定 |
| `@package _global_` / `override /trainer: pi` | **不要**（層が明示なら不要） | — |
| interpolation `${now:...}` / custom resolver | **不要**。§3 の resolver 案は結局「Python で計算した値を YAML 経由で渡す」ための迂回 | 構造化前に Python で計算する（§4 の「純関数」方針と整合） |
| `hydra.job.num` 注入 | **不要**。かつ**実測で `--multirun` 以外では MISSING**（`MissingMandatoryValue: hydra.job.num`）。§3 の resolver 案は multirun 限定でしか成立しない | runner が trial 番号を持っている |
| 出力ディレクトリ管理 | ほぼ不要。MR185 自身が `chdir: false` / `output_subdir: null` / `run.dir: .` で大半を無効化していた | `Path(root)/study_name/f"trial-{n}"` 数行 |
| CLI 上書き構文 | 必要 | 上記 103 行に含む |

### 案 C の総量見積もり

- `ml/config/composition.py` — 100〜120 行（実測 103 行）
- `ml/tuning/study.py` — **案に関係なく §4 が要求する**（study 名生成 / storage 検証 /
  credential 秘匿 / `optimization_results.yaml` / lineage 検証、すべて純関数）
- 探索 runner — 60〜80 行（`create_study` + `optimize` + trial ごとの config 合成）

**案 B との差は実質 1 モジュール（約 100 行）**。案 B でも runner と
`ml/tuning/study.py` は同じだけ書く。

### 新規依存の注意

PyYAML は現在**直接依存ではない**（omegaconf と mlflow 経由の推移依存）。
hydra-core を外すと omegaconf が消えるため、YAML を使うなら `ml-train` に
`pyyaml` を明示追加する必要がある。

**代替：`tomllib`（標準ライブラリ）+ TOML。** リポジトリには既に前例がある
（`src/pcbasm/config.py` が `tomllib` + attrs + cattrs、`machine.toml`、
`src/web/api/config_store.py` が tomlkit）。新規依存ゼロで済み、
かつ後述の層分類の問題も解ける。確認事項 1 に上げる。

---

## 3. Optuna 単体の並列 run 能力（実測）

### RDB storage + 複数プロセスは素で動く

独立した 2 プロセスが 1 つの SQLite study を共有して並列に trial を回せた。

| 構成 | 結果 |
| --- | --- |
| 2 プロセス × 5 trial（1 trial 0.3 秒） | 合計 10 trial、**2.12 秒** |
| 1 プロセス × 10 trial | **3.58 秒** |
| 3 番目のプロセスで `load_if_exists=True` | 同一 study を 14 trial まで継続（resume 成立） |

`study_name` + `storage` + `load_if_exists=True` だけで、study 名の一致による
合流と resume が成立する。MR185 の `n_trials` 減算ロジックは不要（§3 の判断は正しい）。

### Hydra の multirun は並列化しない（決め手）

`hydra/_internal/core_plugins/basic_launcher.py` の `BasicLauncher.launch` は
**単一プロセス内の素の `for` ループ**。ソースを読んで確認した。
sweeper の `n_jobs=2` は Optuna の `ask` をバッチ化するだけで、
実行は逐次（実測ログも `Launching 2 jobs locally` のあと job.num 0 → 1 が順に走る）。

Hydra で本当に並列にするには launcher plugin が要る。

| plugin | 安定版 | 現行 dev |
| --- | --- | --- |
| `hydra-joblib-launcher` | 1.2.0（**2022-05-17**） | 1.4.0.dev9（2026-08-30） |
| `hydra-submitit-launcher` | 1.2.0（**2022-05-17**） | 1.4.0.dev9（2026-08-30） |

安定版は `hydra-core>=1.1.0.dev7` のままの 4 年前の release で、
1.4 系 dev を使えば **pre-release 固定が 4 点目**になる。

### 結論

2 GPU のワークステーションでは次で足りる。

```
CUDA_VISIBLE_DEVICES=0 <train> --study X --storage sqlite:///.../X.db &
CUDA_VISIBLE_DEVICES=1 <train> --study X --storage sqlite:///.../X.db &
```

**要件「並列 run」を満たすのは Optuna + RDB であって Hydra ではない。**
Hydra の multirun はこの要件に何も寄与しない。

---

## 4. MR185 の失敗の再確認

`origin/feature/2026-09-01/paste-volume-ml` の実物を読んだ。4 階層の委譲は事実。

1. `src/hydra_plugins/pcbasm_paste_volume/__init__.py` — `PersistentOptunaSweeper(Sweeper)`
   の遅延委譲。`Implementation.register(type(self))` で ABC 登録まで細工している
2. `src/ml/paste_volume/hpo_sweeper.py` — `study_identity_resolver` / `result_kind` を注入する subclass
3. `src/ml/tuning/hydra_sweeper.py::PersistentOptunaSweeper(Sweeper)` — impl を包む
4. `_PersistentOptunaSweeperImpl(OptunaSweeperImpl)` — `setup` / `sweep` と
   **private の `_configure_trials`** を override、さらに `_study` /
   `_mlflow_run_ids` / `_write_complete_results` を追加

現行規約にも違反している（`@dataclass(frozen=True)`、`raise ValueError` 多用、
英語 docstring、ファイル中間での import）。**そのまま再現しない**という §3 の判断は妥当。

### §3 代替表の成立判定

| MR185 のカスタム | §3 の代替 | 案 A | 案 B | 案 C |
| --- | --- | --- | --- | --- |
| 決定論的 study 名 | OmegaConf custom resolver | **無理がある** | **無理がある** | 自然 |
| trial ごとの checkpoint dir / run 名 | `${hydra:job.num}` | multirun 限定 | multirun 限定 | 自然 |
| resume 時の `n_trials` 減算 | `load_if_exists` | 成立 | 成立 | 成立 |
| MLflow lineage の強制 | 事後の純関数 `verify_study_lineage()` | 成立 | 成立 | 成立 |

4 点のうち後半 2 点はどの案でも等しく成立する。分かれるのは前半 2 点。

**study 名の resolver が案 A / B で無理がある理由。** MR185 の study 名は
`dataset_fingerprint` を要求し、それは `resolve_dataset_inputs(manifest=..., roots=...)`
で**データセットを実際に読んで**得ている（`hpo_sweeper.py::_study_identity`）。
OmegaConf の custom resolver に I/O と副作用を持ち込むことになり、
「設定を解決する」層の責務を超える。案 C なら
`study_name = build_study_name(...)` を Python で計算して
`optuna.create_study(study_name=...)` に渡すだけで済む。

`${hydra:job.num}` は実測で `--multirun` 以外では `MissingMandatoryValue` になる。
単発 run と sweep 内 run で config を共有する設計と噛み合わない。

---

## 5. 推奨案

### 案 C（Hydra なし。attrs config + YAML/TOML 差分 + CLI 上書き、Optuna を直接駆動）

決め手（すべて実測）。

1. **案 A の stock sweeper は安定版 Hydra で動かない。** リポジトリの現在の
   `ml-hpo` 環境で sweep が失敗する。成立させるには hydra-core / omegaconf /
   sweeper の 3 点を 2 週間 cadence の pre-release に固定し、真の並列化のために
   launcher plugin で 4 点目を足す。解除時期は不明（hydra-core 1.4.0 は 2 年以上 dev）。
   「dev 版を 1 つ許す」の想定を大きく超える
2. **Hydra は要件「並列 run」に寄与しない。** `BasicLauncher` は逐次ループ。
   並列は Optuna + RDB が素で提供する（実測 2.12s vs 3.58s、resume も成立）
3. **要件「config 上書き」の自作量は実測 103 行。** strict cattrs 境界（MR4 の資産）と
   §4 の「既定値は attrs のみ」方針が、合成機能の大半を不要にする。
   案 B との差は実質 1 モジュール
4. **MR185 が失敗した表面ごと消える。** private `_impl` の継承、4 階層の委譲、
   `src/hydra_plugins/` namespace package、`_recursive_` 回避策のすべてが不要になる
5. **型検査に有利。** `make type` は `reportImplicitOverride` 有効の pyright。
   `DictConfig` は実質 `Any` で、Hydra 1.4 dev に安定した stub は無い。
   plain dict + attrs frozen なら全経路が型付けされる
6. **依存が減る方向にしか動かない。** `ml-runtime` だけの Raspberry Pi 5 に対して
   安全側（§6 の依存方向の制約と整合）

なお typo 検出（`lerning_rate` の拒否）は Hydra ではなく
strict cattrs の `forbid_extra_keys` が担っている。MR185 のように YAML を
plain（structured config でない）で書く限り Hydra は typo を捕まえない。
**この利点は案 A / B の差別化要因にならない。**

### 採らなかった案を採るべき条件

**案 B を採るべきとき。**
`hydra-core>=1.3,<2`（安定版、dev 固定ゼロ）で config group / custom resolver /
CLI 上書き / `--multirun` の cartesian sweep が動くことは実測済み。次のいずれかなら案 B。

- 多数の config 軸を横断する ad-hoc な cartesian / grid 探索を日常的に回したい
- Hydra の `--help` / tab 補完 / job logging を無償で得たい
- レビューで「103 行の合成層は車輪の再発明」と判断された場合の後退先

**案 A を採るべきとき。**

- `hydra-core==1.4.0` と `hydra-optuna-sweeper==1.4.0` が**同時に final** になった
- かつ Slurm / 分散 launcher（submitit）が必要になった

移行コストは低い。境界（`plain dict → strict cattrs → frozen attrs`）が
3 案で同一なので、案 C → 案 A / B の乗り換えは合成層の差し替えで済む。
**この境界を崩さないことが、案の選定より重要。**

---

## 6. 依存への影響

### `pyproject.toml` の差分

```toml
ml-train = [
    { include-group = "ml-runtime" },
-   "hydra-core>=1.3,<2",
    "mlflow>=3.15,<4",
+   # YAML を採るなら必要。TOML（stdlib tomllib）なら追加不要 → 確認事項 1
+   "pyyaml>=6,<7",
]
ml-hpo = [
    { include-group = "ml-train" },
-   # optuna 4.x に対応する hydra-optuna-sweeper は dev release しか存在しない
-   # （最新安定版 1.2.0 は optuna<3.0.0 を要求する）。
-   "hydra-optuna-sweeper==1.4.0.dev9",
    "optuna>=4.9,<5",
]
```

残す行：`optuna>=4.9,<5`（RDB storage 用の sqlalchemy / alembic を自前で引く。実測確認済み）、
`mlflow>=3.15,<4`、`ml-runtime` / `ml-export` は変更なし。

### `uv.lock`

**更新必須。** CI は `uv sync --locked --all-groups`。`uv lock` を回す。
`hydra-core` / `omegaconf` / `antlr4-python3-runtime`（omegaconf 専用）が lock から抜ける。
`pyyaml` は mlflow 経由で残る。

現状の lock は `hydra-core 1.3.6` / `omegaconf 2.3.1` / `hydra-optuna-sweeper 1.4.0.dev9`
= **実測で壊れている組み合わせ**。案 B / C いずれでも lock の更新でこれが解消される。

### `tests/ml/test_architecture.py` への影響

- `HEAVY_DEPENDENCIES` / `TRAINING_ONLY_DEPENDENCIES` の `"hydra"` / `"omegaconf"` は、
  案 C では install されないため assertion が**空虚になる**（機構を守らない）。
  MR4 レビューの「テストが機構を守っていない」指摘と同種なので、
  この 2 つは削り、`optuna` / `mlflow` / `onnx` 系を残す
- **新規 module の層分類が設計を縛る。**
  `ml.config.composition` は PyYAML を使うなら `DEPENDENCY_FREE_MODULES` に入れられない
  （PyYAML は `ml-runtime` に無く、Pi へ入れたくない）。
  `tomllib`（stdlib）なら `DEPENDENCY_FREE_MODULES` に置ける。
  MR6 で Pi 側が成果物同梱の設定を読む可能性を考えると、後者が明確に有利
- `ml.tuning.study` は `ml-hpo` 層。`RUNTIME_MODULES` には入れない

### Raspberry Pi 5（`ml-runtime` のみ）の推論経路

壊れない。むしろ package が減る方向にしか動かない。
`test_architecture.py` の subprocess 検証がこれを引き続き守る。

### wheel 同梱（§4 のチェック項目）

**確認済み。** `uv build --wheel` して中身を検査したところ、`src/` 配下の
非 `.py` ファイルは既に全部入っている（`html` 33 / `js` 20 / `md` 6 / `wav` 2 /
`css` 1 / `typed` 1）。`src/ml/config/conf/*.yaml`（または `*.toml`）は
`[tool.uv.build-backend] module-name` に `ml` が入っているので**自動で含まれる。
`pyproject.toml` の追加設定は不要。**

---

## 7. 確認事項

1. **設定ファイルの形式を YAML と TOML のどちらにするか。**
   TOML は stdlib `tomllib` で読めて新規依存ゼロ、`src/pcbasm/config.py` +
   `machine.toml` の前例と揃い、`ml-runtime` のみの Pi でも依存フリー層に置ける。
   YAML は ML 界の慣習で、`optimization_results.yaml` を YAML で残すなら
   どちらにせよ `ml-hpo` 層に PyYAML が要る。
   **暫定案：設定層は TOML（stdlib）、探索結果の成果物も TOML。**
   外部ツールが YAML を要求するなら YAML に寄せる。

2. **案 C を採ることの是非（案 B への後退を許容するか）。**
   案 C は約 100 行の合成層を自前で持つ。案 B は安定版 hydra-core だけで
   合成を借り（dev 固定ゼロ）、Optuna は自前 runner で駆動する。
   実測では並列 run も config 上書きも案 C で足りることが確認できた。
   **暫定案：案 C。** レビューで「車輪の再発明」と判断されたら案 B へ後退する
   （境界が同一なので差し替えは合成層のみ）。

3. **strict converter の int / float 厳格さを CLI 境界でどう扱うか。**
   実測で `trainer.learning_rate=1` は
   `invalid value for type, expected float` で拒否される（`weight_decay: 0` も同様）。
   MR4 の資産である strict converter は変えたくない。
   **暫定案：converter はそのままにし、「float フィールドは小数点か指数を必ず書く」
   規約 + テストで固定する。** CLI 境界で int → float を自動昇格させる案もあるが、
   `bool` が `int` として通るのを防いでいる `_exact_type` の意図を薄めるため見送る。

---

## 8. 実測の後始末

`pyproject.toml` / `uv.lock` / `src/` / `tests/` は変更していない（`git status` clean）。

- コンテナ内 `/tmp/hpo-probe`, `/tmp/hpo-probe2`, `/tmp/hpo-probe3`, `/tmp/hpoB`,
  `/tmp/vprobe`, `/tmp/caseC-conf`, `/tmp/wheel-probe`, `/tmp/db_init_lock-*` を作成 → **全て削除済み**
- 依存の試験 install は**すべて `/tmp` 配下の隔離 venv**に対して行った。
  プロジェクト環境 `/opt/venv` にパッケージを追加・削除していない（164 dist、
  `ml-hpo` + `ml-export` が揃ったまま。確認済み）
- リポジトリ直下に一時的に `.probe_composition.py` / `.probe_caseC_test.py` を
  置いて本物の `ml.serialization` に通した → **削除済み**（`git status` clean）
- 案 C の試作 `composition.py` は scratchpad に残してある：
  `/tmp/claude-1001/-data-geson-pcb-assembly/c2ac7621-2633-49fb-8e80-0d7cb7dfaaae/scratchpad/caseC/composition.py`

## 9. 参照

- 引き継ぎ資料：`git show origin/docs/2026-09-07/ml-core-5-handoff:memory/agents/orchestrator/ml-core-5-handoff.md`
- MR185 の実物：`git show origin/feature/2026-09-01/paste-volume-ml:src/ml/tuning/hydra_sweeper.py`
  ほか `src/ml/paste_volume/hpo_sweeper.py`, `src/hydra_plugins/pcbasm_paste_volume/__init__.py`
- 境界にする strict converter：`src/ml/serialization.py`
- 層の機械検証：`tests/ml/test_architecture.py`
- 既存 TOML config の前例：`src/pcbasm/config.py`, `src/web/api/config_store.py`
- 規約：`AGENTS.md`, `CLAUDE.md`

---
---

# 実装計画（決定後）

## 10. 確定した決定

§1〜§9 は選定時の判断材料として残す。ユーザーが下した決定は次の 3 点で、**覆さない**。

1. **案 C を採用。** Hydra なし。`hydra-core` / `hydra-optuna-sweeper` を
   `pyproject.toml` から削り、Optuna を直接駆動する
2. **設定ファイルは TOML。** stdlib `tomllib`。新規依存ゼロ。
   `src/pcbasm/config.py` + `machine.toml` の前例に揃える
3. **int / float は規約で縛る。** `ml/serialization.py` の strict converter は
   **一切変更しない**。「float フィールドは小数点か指数を必ず書く」を規約にし、
   `learning_rate = 1` が拒否されることをテストで pin する。
   合成層での int → float 昇格は採らない

---

## 11. スコープの判断：runner を MR5 に含める

**含める。** ただし「プロセスを起動する部分」は含めない。

### 理由

- ユーザーの要件は「並列 run と config 上書きが**効率的にできること**」。
  合成層と純関数だけを入れて runner を MR6 へ回すと、MR5 の成果物では
  **1 度も探索が走らない**。要件が満たされたかを検証できない
- 一方、並列化の実体は「複数 OS プロセスが 1 つの RDB study を共有する」ことであり
  （§3 で実測）、**プロセスを起こすのは運用者（shell / `CUDA_VISIBLE_DEVICES`）**。
  プロセス supervisor を書くのは AGENTS.md 開発原則 2「要求されていない機能・
  柔軟性を追加しない」に反する。MR185 が 4 階層の委譲に膨らんだのも同じ病
- したがって `ml/` に入れるのは **「共有 study に trial を積む 1 プロセス分のループ」**
  だけ。これは薄く、プロセス起動を伴わないのでそのままテストできる

### 並列 run 要件が満たされたと言える条件

`ml/tuning/runner.py` の `HyperparameterSearch` が、次を満たすことをテストで示す。

1. 同じ `study_name` + `storage` を指す**2 つの `HyperparameterSearch` インスタンス**が
   同一 study に trial を積み、合計 trial 数が両者の和になる（合流）
2. 3 つ目のインスタンスが `load_if_exists` で既存 study を継続する（resume）
3. trial ごとに独立した `TrialAssignment`（run 名 / checkpoint 先が衝突しない）が渡る

プロセスを実際に 2 つ起こす検証は、**同一プロセス内の 2 インスタンス + 同一 SQLite
storage** で置き換える。SQLite ファイル経由の合流はプロセス境界と同じ経路を通るため、
機構としては同じものを検証できる。真の 2 プロセス並列は運用手順として README へ書く
（`docker/README.md` ではなく `src/ml/tuning/` の docstring と MR5 の MR 説明）。

---

## 12. 追加・変更するファイル

```
src/ml/config/__init__.py            新規（docstring のみ。re-export しない）
src/ml/config/composition.py         新規（stdlib tomllib のみ → 依存フリー層）
src/ml/config/packaged.py            新規（wheel 同梱 conf の所在。依存フリー層）
src/ml/config/conf/trainer/edge.toml 新規（同梱する唯一の profile）
src/ml/tuning/__init__.py            新規（docstring のみ）
src/ml/tuning/study.py               新規（optuna を import しない → 依存フリー層）
src/ml/tuning/search_space.py        新規（optuna を import する → ml-hpo 層）
src/ml/tuning/runner.py              新規（optuna を import する → ml-hpo 層）
pyproject.toml                       変更（依存グループ）
uv.lock                              変更（再生成）
tests/ml/test_architecture.py        変更（層登録と assertion の実効化）
tests/ml/config/__init__.py          新規
tests/ml/config/test_composition.py  新規
tests/ml/config/test_packaged.py     新規
tests/ml/tuning/__init__.py          新規
tests/ml/tuning/test_study.py        新規
tests/ml/tuning/test_search_space.py 新規
tests/ml/tuning/test_runner.py       新規
tests/ml/tuning/test_integration.py  新規（合成 → 探索 → 成果物の通し）
```

**層の設計判断。** `ml.tuning.study` から optuna を追い出し、`ml.config.*` を
stdlib だけにしたことで、この 3 module は `DEPENDENCY_FREE_MODULES`（`ml-runtime`
すら不要）に入る。`StudyIdentity.build` が `SearchSpace` ではなく
`search_space_fingerprint: str` を受け取るのはこのため（`search_space.py` は
optuna を import するので、`study.py` から参照すると層が壊れる）。

---

## 13. 公開インターフェース案（シグネチャ確定）

以下は `spec-test-author` と `plan-implementer` を並列起動できる粒度まで確定させたもの。
**引数名・keyword-only の別・戻り値型を変えないこと。** 変える必要が出たら
orchestrator へ差し戻す。

### 13.1 `src/ml/config/composition.py`

```python
type ConfigMapping = Mapping[str, object]

@attrs.frozen
class ConfigComposition:
    layer_paths: tuple[Path, ...] = ()
    overrides: tuple[str, ...] = ()

    @classmethod
    def from_arguments(
        cls,
        arguments: Sequence[str],
        *,
        configuration_root: Path,
        base_names: Sequence[str] = (),
    ) -> tuple[ConfigComposition | None, str | None]: ...

    def validate(self) -> str | None: ...

    def compose(self) -> tuple[dict[str, object] | None, str | None]: ...

    def structure[T](
        self,
        target: type[T],
        *,
        converter: Converter,
    ) -> tuple[T | None, str | None]: ...
```

- `from_arguments` の振り分け規則：token `name=value` の `name` が
  `configuration_root / name` という**ディレクトリとして実在すれば group 選択**
  （層 `configuration_root / name / value.toml` を積む）。実在しなければ**上書き**。
  この規則にしたのは、`run_kind=finetune` のような非 dotted の上書きと
  group 選択を曖昧にしないため（dotted 有無で判定する案は誤読する）
- 層の順序：`base_names` の TOML（`configuration_root / f"{name}.toml"`）→
  引数に現れた順の group 層 → 上書き
- `compose` の merge 規則：後の層優先で mapping を再帰 merge、**list は置換**
- `overrides` の値は `tomllib` と同じ規則で解釈する。実装は
  `tomllib.loads(f"value = {raw}")` を通し、失敗したら理由を返す
  （TOML の scalar 規則を 1 箇所に保つ。自前の scalar parser を書かない）
- `structure` は `structure_strictly` を呼ぶだけ。**converter は生成せず受け取る**
  （`make_strict_converter` の呼び出し箇所を増やさない）
- 失敗はすべて `str | None` の理由。例外を投げない

### 13.2 `src/ml/config/packaged.py`

```python
@attrs.frozen
class PackagedConfiguration:
    root: Path

    @classmethod
    def locate(cls) -> PackagedConfiguration: ...

    def validate(self) -> str | None: ...

    def group_names(self) -> tuple[str, ...]: ...

    def option_names(self, group: str) -> tuple[str, ...]: ...
```

- `locate` は `Path(__file__).parent / "conf"` を指す。`importlib.resources` は使わない
  （wheel は zip ではなく展開して install されるため不要。`src/pcbasm/config.py` も
  素の `Path` を使っている）
- `group_names` / `option_names` は整列済み tuple。`option_names` は `.toml` を外した名前

### 13.3 `src/ml/tuning/study.py`（optuna を import しない）

```python
type Direction = Literal["minimize", "maximize"]

@attrs.frozen
class StudyIdentity:
    model_family: str
    dataset_fingerprint: str
    search_space_fingerprint: str

    @classmethod
    def build(
        cls,
        *,
        model_family: str,
        dataset_fingerprint: str,
        search_space_fingerprint: str,
    ) -> tuple[StudyIdentity | None, str | None]: ...

    def validate(self) -> str | None: ...

    @property
    def study_name(self) -> str: ...


@attrs.frozen
class StudyStorage:
    uri: str

    def validate(self) -> str | None: ...

    @property
    def redacted_uri(self) -> str: ...


@attrs.frozen
class TrialRecord:
    number: int
    state: str
    value: float | None
    parameters: Mapping[str, Scalar]
    experiment_run_id: str | None = None

    def validate(self) -> str | None: ...

    @property
    def is_complete(self) -> bool: ...


@attrs.frozen
class StudyResults:
    study_name: str
    storage_uri_redacted: str
    direction: Direction
    search_space_fingerprint: str
    trials: tuple[TrialRecord, ...]

    DOCUMENT_KIND: ClassVar[DocumentKind] = DocumentKind(
        kind="ml-hyperparameter-search-results", schema_version=1
    )

    @classmethod
    def build(
        cls,
        *,
        identity: StudyIdentity,
        storage: StudyStorage,
        direction: Direction,
        trials: Sequence[TrialRecord],
    ) -> tuple[StudyResults | None, str | None]: ...

    def validate(self) -> str | None: ...

    @property
    def best_trial(self) -> TrialRecord | None: ...

    @property
    def completed_trial_count(self) -> int: ...

    def verify_lineage(self) -> str | None: ...

    def save(self, path: Path, *, converter: Converter) -> None: ...

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        converter: Converter,
    ) -> tuple[StudyResults | None, str | None]: ...
```

- `study_name` は `f"{safe_model_family}-{dataset[:12]}-{space[:12]}"`。
  `sha256:` 接頭辞を外し、`[^a-zA-Z0-9_.-]+` を `-` へ潰す（MR185 の規則を踏襲）
- `StudyStorage.validate` は永続 storage のみ許す。`sqlite:///` は**絶対パス必須**、
  `postgresql` / `postgresql+psycopg` / `mysql` は host と database 名が必要。
  in-memory（URI 無指定）は拒否。理由文字列で返す（MR185 は `raise` していた）
- `redacted_uri` は credential を落とす。**`validate` が通らない URI では
  呼ばない前提にせず、`validate` 相当の判定を内部で通してから落とす**
- `verify_lineage` は **`str | None` を返す純関数**。COMPLETE な trial に
  `experiment_run_id` が無ければその trial 番号を列挙して返す。§3 の
  `verify_study_lineage()` をクラスのメソッドにしたのは
  「クラスに属する関数は module-level に置かず classmethod にする」規約に沿わせるため
- `save` / `load` は `DocumentKind` 経由。**成果物は JSON**（`atomic_write_json`）。
  設定入力は TOML、成果物は既存の `DocumentKind` に揃えて JSON とする
  （`ml/artifact/document.py` の資産を二重化しない）

### 13.4 `src/ml/tuning/search_space.py`（optuna を import する）

```python
type DistributionKind = Literal["float", "integer", "categorical"]

@attrs.frozen
class ParameterDistribution:
    kind: DistributionKind
    low: float | None = None
    high: float | None = None
    log: bool = False
    step: int | None = None
    choices: tuple[Scalar, ...] = ()

    def validate(self) -> str | None: ...

    def suggest(self, trial: optuna.trial.Trial, *, name: str) -> Scalar: ...


@attrs.frozen
class SearchSpace:
    parameters: Mapping[str, ParameterDistribution]

    def validate(self) -> str | None: ...

    @property
    def fingerprint(self) -> str: ...

    def suggest(self, trial: optuna.trial.Trial) -> dict[str, Scalar]: ...
```

- **tagged union にしない。** 1 クラス + `kind` + `validate()` で、kind ごとに
  必要なフィールドが揃っているかを理由文字列で返す。strict converter に union の
  structure hook を足さずに済ませるため（converter は変更しないという決定 3 に沿う）
- `validate` の内容：`float` / `integer` は `low` / `high` 必須かつ `low < high`、
  `choices` は空でなければならない（`categorical`）／空でなければならない（他は空必須）。
  `log` は `float` / `integer` のみ、`low > 0` を要求。`step` は `integer` のみ
- `fingerprint` は `fingerprint_json` に `attrs.asdict` を通した値。
  **キー順に依存しない**（`canonical_json` が `sort_keys=True`）
- `SearchSpace.parameters` のキーは `ConfigComposition` の上書きと同じ
  **dotted config path**（例 `trainer.learning_rate`）。TOML では
  `[search_space."trainer.learning_rate"]` と引用キーで書く
- `suggest` の戻り値は dotted key → 値。`ParameterDistribution.suggest` は
  `suggest_float` / `suggest_int` / `suggest_categorical` へ振り分ける

### 13.5 `src/ml/tuning/runner.py`（optuna を import する）

```python
@attrs.frozen
class TrialAssignment:
    study_name: str
    trial_number: int
    overrides: Mapping[str, Scalar]

    @property
    def run_name(self) -> str: ...

    def as_tags(self) -> dict[str, str]: ...

    def as_override_arguments(self) -> tuple[str, ...]: ...


type TrialObjective = Callable[[TrialAssignment], float]


@attrs.frozen
class HyperparameterSearch:
    identity: StudyIdentity
    storage: StudyStorage
    search_space: SearchSpace
    direction: Direction = "minimize"
    trial_count: int = 20

    def validate(self) -> str | None: ...

    def run(
        self,
        objective: TrialObjective,
    ) -> tuple[StudyResults | None, str | None]: ...

    def collect(
        self,
        *,
        experiment_run_ids: Mapping[int, str] | None = None,
    ) -> tuple[StudyResults | None, str | None]: ...
```

- `run_name` は `f"{study_name}-trial-{trial_number}"`
- `as_tags` は `hpo.` 前置きの tag を返す：`hpo.study_name` / `hpo.trial_number`。
  **これは param ではなく tag。** MR4 の `TrainerConfig.as_tags()` が
  `training.` 前置きなのと同じ設計（§16 の対応表で機構を pin する）
- `as_override_arguments` は `("trainer.learning_rate=0.0001", ...)` を返す。
  `ConfigComposition.overrides` へそのまま渡せる形にして、
  探索と単発 run の config 経路を 1 本に保つ
- `run` は `optuna.create_study(study_name=..., storage=..., direction=...,
  load_if_exists=True)` を作り、`study.optimize(..., n_trials=self.trial_count)` を回す。
  **`n_jobs` は渡さない**（既定 1）。並列は複数プロセスで達成する（§11）
- `run` は `trial_count` を**減算しない**。既存 trial に積み増す（§3 の判断）
- `collect` は study を読むだけで trial を回さない。`experiment_run_ids` を渡すと
  `TrialRecord.experiment_run_id` を埋める。MLflow から run id を引くのは
  `ml/tuning/` の外（呼び出し側）。**`ml.tuning` は mlflow を import しない**

---

## 14. 実装ステップ（依存順・commit 境界）

### commit 1 — `chore(ml): Hydra 依存を外す`

1. `pyproject.toml`
   - `ml-train` から `"hydra-core>=1.3,<2"` を**削る**
   - `ml-train` に**何も足さない**（TOML は stdlib）。`"mlflow>=3.15,<4"` は残す
   - `ml-hpo` から `"hydra-optuna-sweeper==1.4.0.dev9"` とその 2 行コメントを**削る**
   - `ml-hpo` の `"optuna>=4.9,<5"` は**残す**。`ml-runtime` / `ml-export` は無変更
2. `uv lock` を回して `uv.lock` を再生成。CI は `uv sync --locked --all-groups`
   なので lock 更新は必須。`hydra-core` / `omegaconf` / `antlr4-python3-runtime`
   （omegaconf 専用）が lock から抜ける。`pyyaml` は mlflow 経由で残る
3. コンテナへ反映：`make ml-docker-sync`。`uv sync` は削除された package を
   環境から**落とす**ので、`hydra` が本当に import できなくなることを確認する
4. `tests/ml/test_architecture.py`
   - `HEAVY_DEPENDENCIES` から `"hydra"` / `"omegaconf"` を**削る**
   - `TRAINING_ONLY_DEPENDENCIES` から `"hydra"` / `"omegaconf"` を**削る**
   - 残す：`"mlflow"` / `"onnx"` / `"onnxscript"` / `"onnxruntime"` / `"optuna"` /
     `"torch"` / `"torchvision"`
   - **理由をコメントで残す**：install されない名前を forbidden に置くと
     assertion が空虚になり、機構を守らないテストになる（MR4 レビューの指摘と同種）
5. `scripts/ml_smoke.py`（**grep で発見。見落としやすい**）
   - `_REPORTED_PACKAGES`（38 行目付近）から `"hydra-core"` を**削る**。
     `check_versions` は未 install を `report.skip` にするので失敗はしないが、
     永久に skip される行が残る。§14 commit 1 の 4 と同じ「空虚な検査」になる
   - `main()` の `report.skip("hydra compose", "packaged config ... は未実装")`
     （326 行目付近）を**削る**。MR5 は packaged config を実装するので、
     これは §20 の実 check へ置き換える（置き換え自体は commit 2）
   - `scripts/ml_smoke.py` は `ML_TYPE_PATHS` に入っているので `make type` の対象
6. 検証：`make ml-docker-check`。ベースライン 592 passed / 1 skipped が維持されること。
   併せて `make ml-docker-smoke` が通ること（`hydra-core` の行が消え、
   `hydra compose` の skip が無くなる）

この commit は **`src/` を触らない**。`src/` に hydra / omegaconf の import は
無いことを grep で確認済み（唯一の参照が上記 `scripts/ml_smoke.py` の 2 箇所）。
`docker/`・`Makefile`・`.gitlab-ci.yml` に hydra 参照は無い（§18 リスク 6 は解消済み）。

### commit 2 — `feat(ml): TOML 設定合成の境界を追加する`

`src/ml/config/__init__.py`、`composition.py`、`packaged.py`、`conf/trainer/edge.toml`。
テストは `tests/ml/config/`。`tests/ml/test_architecture.py` の
`DEPENDENCY_FREE_MODULES` に `ml.config.composition` / `ml.config.packaged` を追加。
併せて `scripts/ml_smoke.py` へ §20 の `check_packaged_configuration` を追加し、
commit 1 で削った `hydra compose` の skip を実 check へ置き換える。

### commit 3 — `feat(ml): 探索 study の同一性と成果物を追加する`

`src/ml/tuning/__init__.py`、`study.py`。テストは `tests/ml/tuning/test_study.py`。
`DEPENDENCY_FREE_MODULES` に `ml.tuning.study` を追加。

### commit 4 — `feat(ml): 探索空間の宣言を追加する`

`src/ml/tuning/search_space.py`。テストは `tests/ml/tuning/test_search_space.py`。
`RUNTIME_MODULES` には**入れない**（optuna は `ml-hpo` 層）。

### commit 5 — `feat(ml): 共有 study へ trial を積む runner を追加する`

`src/ml/tuning/runner.py`。テストは `tests/ml/tuning/test_runner.py`。

### commit 6 — `test(ml): 合成と探索の通し検証と二重管理の禁止を pin する`

`tests/ml/tuning/test_integration.py` と、§15.6 の横断ガード。

commit 2〜5 は依存順（2 → 3 → 4 → 5）。3 と 4 は互いに独立なので、
`spec-test-author` と `plan-implementer` を並列で走らせる場合はここで分岐できる。

---

## 15. テスト観点

区分は skill `testing-strategy` の 4 区分に従う。実機は関係ないので
`@mark_hardware` は使わない。**モックは書かない**（optuna は 3rd-party だが、
SQLite storage を `tmp_path` に置いて実物を使う。`sqlite:///` は実ファイルなので
fake 不要）。

### 15.1 `tests/ml/config/test_composition.py`（unit）

`class TestComposeLayers`
- 正常系：層ゼロ・上書きゼロで空 dict を返す
- 正常系：1 層の TOML を読んで dict になる
- 正常系：2 層の差分が後勝ちで再帰 merge される（片方だけが持つキーは残る）
- 正常系：list は merge されず**置換**される
- 異常系：存在しない層のパスで理由を返す（例外を投げない）
- 異常系：TOML として壊れたファイルで理由を返す
- 異常系：最上位が table でない TOML で理由を返す

`class TestApplyOverrides`
- 正常系：dotted 上書きが既存の値を差し替える
- 正常系：dotted 上書きが**存在しない中間 table を作る**
- 正常系：値が TOML の規則で解釈される（`true` → bool、`1.0e-4` → float、
  `"x"` → str、`[1, 2]` → list）
- 異常系：`=` を含まない token で理由を返す
- 異常系：`=` の左が空の token で理由を返す
- 異常系：値が TOML として解釈できないとき理由を返す
- 異常系：中間キーが table でないとき（`a=1` のあと `a.b=2`）理由を返す

`class TestFromArguments`
- 正常系：`configuration_root` にディレクトリが実在する token が group 選択になる
- 正常系：ディレクトリが実在しない token が上書きになる（`run_kind=finetune`）
- 正常系：`base_names` の層が group 層より**先**に積まれる
- 正常系：group 層が引数に現れた順に積まれる
- 異常系：group が実在するが option の TOML が無いとき理由を返す

`class TestStructure`
- 正常系：既定値のみ（層も上書きも無し）で attrs の既定値どおりに構造化される
- 正常系：差分 TOML + 上書きが frozen attrs へ落ちる
- 異常系：未知キー（`lerning_rate`）が `forbid_extra_keys` で拒否される
- **異常系：`learning_rate = 1`（int）が拒否される** ← 決定 3 の pin。
  理由文字列に `expected float` 相当が含まれること
- 異常系：`learning_rate = 1.0` は通ること（対照。規約「小数点を必ず書く」の根拠）

### 15.2 `tests/ml/config/test_packaged.py`（integration-with-fakes ではなく unit）

`class TestPackagedConfiguration`
- 正常系：`locate()` の `root` が実在するディレクトリを指す
- 正常系：`group_names()` に `"trainer"` が含まれる
- 正常系：`option_names("trainer")` に `"edge"` が含まれる
- 正常系：`option_names` / `group_names` が整列済みである
- 異常系：存在しない group で `option_names` が空 tuple を返す
- 異常系：`root` が無い `PackagedConfiguration` の `validate()` が理由を返す
- **`conf/**/*.toml` の全ファイルが `TrainerConfig` 等へ strict に構造化できる**
  （= TOML のキー ⊆ attrs のフィールド。§15.6 と重複させず、ここでは
  同梱ファイルだけを対象にする）

### 15.3 `tests/ml/tuning/test_study.py`（unit）

`class TestStudyIdentity`
- 正常系：同じ 3 要素から同じ `study_name` が出る（決定論）
- 正常系：`model_family` の記号が `-` へ潰され、fingerprint が 12 桁に切られる
- 正常系：`dataset_fingerprint` の `sha256:` 接頭辞が落ちる
- 正常系：3 要素のどれか 1 つが変わると `study_name` が変わる（3 ケース parametrize）
- 異常系：`model_family` が空／記号のみで `build` が理由を返す
- 異常系：`dataset_fingerprint` / `search_space_fingerprint` が空で理由を返す

`class TestStudyStorage`
- 正常系：絶対パスの `sqlite:///` が通る
- 正常系：`postgresql://host/db`、`postgresql+psycopg://host/db`、`mysql://host/db` が通る
- 異常系：相対パスの `sqlite:///` が理由を返す
- 異常系：空文字が理由を返す
- 異常系：`sqlite://`（in-memory）が理由を返す
- 異常系：未知 scheme（`redis://`）が理由を返す
- 異常系：host / database を欠く server URI が理由を返す
- 正常系：`redacted_uri` が password を落とす
  （`postgresql://user:secret@host/db` に `secret` が**含まれない**）
- 正常系：`redacted_uri` が port を保つ

`class TestStudyResults`
- 正常系：`build` が `identity.study_name` と `storage.redacted_uri` を埋める
- 正常系：`best_trial` が direction に従って選ばれる（minimize / maximize を parametrize）
- 正常系：`best_trial` は COMPLETE 以外を候補にしない
- 正常系：trial が空／COMPLETE ゼロで `best_trial` が None
- 正常系：`completed_trial_count` が COMPLETE のみを数える
- 正常系：`save` → `load` で往復する（`DocumentKind` エンベロープ付き）
- 異常系：`load` が未知 `kind` / 未対応 `schema_version` で理由を返す
- **異常系：`save` した JSON に credential が現れない**
  （`storage_uri_redacted` しか持たないことを、生の文字列検索で確認）
- 異常系：trial 番号が重複／負で `validate` が理由を返す

`class TestVerifyLineage`
- 正常系：全 COMPLETE trial に `experiment_run_id` があれば `None`
- 異常系：COMPLETE trial の `experiment_run_id` が欠けると、**その trial 番号を
  含む**理由を返す
- 正常系：FAIL / PRUNED trial の `experiment_run_id` 欠落は許す

### 15.4 `tests/ml/tuning/test_search_space.py`（integration-with-fakes：実 optuna trial を使う）

`class TestParameterDistributionValidation`
- 異常系 parametrize：`float` で `low` 欠落／`high` 欠落／`low >= high`／
  `log=True` かつ `low <= 0`／`choices` が非空
- 異常系 parametrize：`integer` で `step <= 0`
- 異常系 parametrize：`categorical` で `choices` 空／`low` や `high` が設定済み
- 正常系：各 kind の妥当な組み合わせが `None` を返す

`class TestSuggest`
- 正常系：`float` が `[low, high]` に入る値を返す
- 正常系：`log=True` で対数スケールの値が返る（範囲内であること）
- 正常系：`integer` が int を返し、`step` の倍数刻みになる
- 正常系：`categorical` が `choices` のいずれかを返す
- 正常系：`SearchSpace.suggest` が全 dotted キーを埋めた dict を返す

`class TestSearchSpaceFingerprint`
- 正常系：`parameters` の**挿入順が違っても同じ fingerprint**
- 正常系：分布の値を 1 つ変えると fingerprint が変わる
- 正常系：`log` の真偽を変えると fingerprint が変わる

`class TestSearchSpaceFromToml`
- 正常系：`[search_space."trainer.learning_rate"]` 形式の TOML が
  `ConfigComposition` + strict converter 経由で `SearchSpace` になる
- 異常系：未知フィールドを持つ分布 TOML が拒否される

### 15.5 `tests/ml/tuning/test_runner.py`（integration-with-fakes：実 optuna + `tmp_path` の SQLite）

`class TestTrialAssignment`
- 正常系：`run_name` が `{study_name}-trial-{n}`
- 正常系：`as_tags()` のキーが全て `hpo.` 前置き
- 正常系：`as_override_arguments()` が `ConfigComposition.overrides` へ渡せる形
  （実際に `ConfigComposition` へ食わせて `compose()` が成功すること）

`class TestHyperparameterSearchRun`
- 正常系：`trial_count` 個の trial が積まれ、objective が同じ回数呼ばれる
- 正常系：objective が受け取る `TrialAssignment.trial_number` が 0 から連番
- 正常系：objective が受け取る `overrides` のキーが `search_space.parameters` と一致
- 正常系：戻りの `StudyResults` の `study_name` が `identity.study_name` と一致
- **正常系（合流）：同じ `identity` + `storage` の 2 つの `HyperparameterSearch`
  を順に `run` すると、study の trial 数が両者の和になる** ← §11 の要件 1
- **正常系（resume）：3 つ目が `load_if_exists` で既存 study を継続し、
  trial 番号が衝突しない** ← §11 の要件 2・3
- 正常系：`run` が `trial_count` を減算せず、常に積み増す
- 異常系：`validate()` が理由を返す構成（`trial_count <= 0`、不正 storage、
  空 `search_space`）で `run` が理由を返し、**study を作らない**
- 異常系：objective が例外を投げた trial が FAIL として記録され、
  `run` 全体は理由を返さない（探索は続く）

`class TestCollect`
- 正常系：`collect` が trial を回さずに既存 study を読む（objective 呼び出しゼロ）
- 正常系：`experiment_run_ids` を渡すと `TrialRecord.experiment_run_id` が埋まる
- 正常系：`experiment_run_ids` 省略時は `None` のまま
- 異常系：存在しない study 名で `collect` が理由を返す

### 15.6 `tests/ml/tuning/test_integration.py`（integration-with-fakes：横断ガード）

`class TestDefaultsAreNotDuplicated`
- **同梱 `conf/**/*.toml` のどのキーも、対応する attrs の既定値と等しい値を
  書いていない。** 等しければ「既定値の二重管理」なので落とす。
  これが §4 の「既定値は attrs にのみ置き TOML は差分だけ」を機械で守る唯一の機構
- 同梱 TOML が空 table でない（差分ゼロのファイルを置かない）

`class TestFingerprintClassificationSurvivesComposition`
- **時間予算 4 フィールド（`deadline_seconds` / `finalization_grace_seconds` /
  `checkpoint_interval_steps` / `checkpoint_interval_seconds`）だけが異なる
  2 つの TOML 層から合成した `TrainerConfig` の `fingerprint` が一致する**
- **`learning_rate` だけが異なる 2 つの層から合成した `fingerprint` は一致しない**
- 合成経由の `TrainerConfig.as_params()` に時間予算 4 フィールドが**現れない**
- 合成経由の `TrainerConfig.as_tags()` に時間予算 4 フィールドが `training.`
  前置きで**現れる**

`class TestSearchedValuesDoNotEnterParams`
- **`TrialAssignment.as_tags()` のキーが `TrainerConfig.as_params()` のキーと
  交わらない。** HPO メタデータ（study 名 / trial 番号）が param 側へ漏れると
  resume で MLflow が拒否するため
- 探索された `learning_rate` は param 側に載る（trial 内で不変だから正しい）。
  この対照ケースも書き、両者の区別を明示する

`class TestComposeThenSearch`
- 正常系：`PackagedConfiguration.locate()` → `ConfigComposition.from_arguments`
  → `structure` → `HyperparameterSearch.run` → `StudyResults.save` の通し
- 正常系：保存した成果物を `load` して `verify_lineage()` が通る
  （`experiment_run_ids` を渡した場合）

### 15.7 `tests/ml/test_architecture.py`（変更）

- `DEPENDENCY_FREE_MODULES` に追加：`ml.config.composition`、`ml.config.packaged`、
  `ml.tuning.study`
- `HEAVY_DEPENDENCIES` / `TRAINING_ONLY_DEPENDENCIES` から `"hydra"` / `"omegaconf"` を削除
- `RUNTIME_MODULES` は無変更（`ml.tuning.search_space` / `ml.tuning.runner` は
  `ml-hpo` 層なので**入れない**）
- 既存の `test_no_module_imports_a_domain_package` が新 module も自動で走査する
  （`rglob` なので追加不要）

---

## 16. テスト ↔ 潰す機構の対応表

**実装フェーズでこれを実測すること。** 各行の「潰す操作」を施して、
対応するテストが**実際に落ちる**ことを確認し、確認後に必ず戻す。
MR4 では変異を戻さずエージェントが kill される事故があったので、
**変異実験のあとは毎回 `git status` と `git diff` を見る。**

| # | テスト | 潰す機構（この操作でそのテストが落ちる） |
| --- | --- | --- |
| 1 | `TestComposeLayers::test_...後勝ちで再帰merge` | `_merge_into` の再帰分岐（`isinstance(existing, dict) and isinstance(value, Mapping)`）を消して素の `dict.update` にする |
| 2 | `TestComposeLayers::test_...listは置換` | 上と同じ分岐に list を含めて list を連結するようにする |
| 3 | `TestComposeLayers::test_...壊れたTOML` | `tomllib.TOMLDecodeError` の捕捉を外す（例外が漏れて理由が返らない） |
| 4 | `TestComposeLayers::test_...最上位がtableでない` | `isinstance(loaded, dict)` の判定を削る |
| 5 | `TestApplyOverrides::test_...中間tableを作る` | `_assign` の `if child is None: node[part] = {}` を削る |
| 6 | `TestApplyOverrides::test_...中間キーがtableでない` | `_assign` の `if not isinstance(child, dict): return ...` を削る |
| 7 | `TestApplyOverrides::test_...TOML規則で解釈` | `tomllib.loads(f"value = {raw}")` を `str(raw)` そのまま返すように変える |
| 8 | `TestApplyOverrides::test_...=を含まない` | `if not separator or not key: return ...` を削る |
| 9 | `TestFromArguments::test_...ディレクトリ実在でgroup選択` | 判定を「`.` を含むか」に変える（`run_kind=finetune` が group 扱いになる） |
| 10 | `TestFromArguments::test_...base_namesが先` | 層の連結順を group → base に入れ替える |
| 11 | `TestStructure::test_...未知キー拒否` | `ConfigComposition.structure` が `structure_strictly` ではなく素の `converter.structure` を呼ぶ／`make_strict_converter` 以外の converter を既定にする |
| 12 | **`TestStructure::test_...learning_rate=1が拒否`** | 合成層に int → float 昇格を入れる（決定 3 に反する変更の検出器） |
| 13 | `TestPackagedConfiguration::test_...locateが実在` | `conf/` を `module-name` の外へ移す／`locate` のパス計算を 1 段ずらす |
| 14 | `TestPackagedConfiguration::test_...option_namesにedge` | `conf/trainer/edge.toml` を削除する |
| 15 | `TestStudyIdentity::test_...3要素のどれかで変わる` | `study_name` の組み立てから該当要素を落とす（`search_space_fingerprint` を外すのが最も見落としやすい） |
| 16 | `TestStudyIdentity::test_...記号が潰される` | `re.sub(r"[^a-zA-Z0-9_.-]+", "-", ...)` を削る |
| 17 | `TestStudyStorage::test_...相対sqliteを拒否` | `database_path.is_absolute()` の判定を削る |
| 18 | `TestStudyStorage::test_...in-memoryを拒否` | scheme の許可リスト判定を削る |
| 19 | **`TestStudyStorage::test_...redacted_uriがpasswordを落とす`** | `redacted_uri` が `self.uri` をそのまま返すようにする |
| 20 | `TestStudyStorage::test_...portを保つ` | `redacted_uri` の port 連結を落とす |
| 21 | `TestStudyResults::test_...best_trialがdirectionに従う` | `best_trial` を常に `min` にする（maximize ケースが落ちる） |
| 22 | `TestStudyResults::test_...best_trialがCOMPLETE以外を選ばない` | `is_complete` による絞り込みを削る |
| 23 | **`TestStudyResults::test_...JSONにcredentialが現れない`** | `StudyResults` に `storage_uri` 生フィールドを足して `build` で埋める |
| 24 | `TestStudyResults::test_...saveloadの往復` | `DOCUMENT_KIND.schema_version` を上げて `load` 側だけ据え置く |
| 25 | **`TestVerifyLineage::test_...run_id欠落で理由`** | `verify_lineage` を常に `None` を返すようにする（MR185 が sweep 内で `raise` していた検査の代替なので、ここが空洞化すると lineage 保証が消える） |
| 26 | `TestVerifyLineage::test_...FAILの欠落は許す` | 絞り込みを外して全 trial に run id を要求する |
| 27 | `TestParameterDistributionValidation::各異常系` | `validate()` の該当分岐を 1 つずつ削る（parametrize の該当ケースだけが落ちる） |
| 28 | `TestSuggest::test_...integerがstep刻み` | `suggest_int` へ `step` を渡すのを止める |
| 29 | `TestSuggest::test_...log=Trueで対数` | `suggest_float` へ `log` を渡すのを止める |
| 30 | **`TestSearchSpaceFingerprint::test_...挿入順が違っても同じ`** | `fingerprint` を `canonical_json` ではなく `json.dumps(..., sort_keys=False)` にする |
| 31 | `TestSearchSpaceFingerprint::test_...logを変えると変わる` | `fingerprint` の対象から `log` を落とす |
| 32 | `TestTrialAssignment::test_...as_tagsが全てhpo.前置き` | 前置きを外す |
| 33 | `TestTrialAssignment::test_...as_override_argumentsが渡せる` | 区切りを `=` から `:` に変える |
| 34 | **`TestHyperparameterSearchRun::test_...合流`** | `create_study` の `load_if_exists=True` を外す（2 つ目が新規 study を作るか例外になる） |
| 35 | **`TestHyperparameterSearchRun::test_...resume`** | 同上、および `trial_count` を残 trial 数へ減算するロジックを足す（MR185 の再現を検出する） |
| 36 | `TestHyperparameterSearchRun::test_...validate失敗でstudyを作らない` | `run` の先頭の `if error := self.validate(): return None, error` を削る |
| 37 | `TestHyperparameterSearchRun::test_...objective例外がFAIL` | optuna の `catch` 指定／例外処理を変えて `run` 全体を失敗にする |
| 38 | `TestCollect::test_...trialを回さない` | `collect` が `study.optimize` を呼ぶようにする |
| 39 | **`TestDefaultsAreNotDuplicated`** | `conf/trainer/edge.toml` に attrs の既定値と同じ値の行を 1 行足す |
| 40 | **`TestFingerprintClassificationSurvivesComposition::test_...時間予算だけ違えば一致`** | `ml/training/loop.py` の `_FINGERPRINT_EXCLUDED_FIELDS` から 1 要素を削る |
| 41 | **`TestFingerprintClassificationSurvivesComposition::test_...learning_rateで不一致`** | `fingerprint` を定数にする |
| 42 | `TestFingerprintClassificationSurvivesComposition::test_...as_tagsに時間予算` | `as_tags` の返り値を空 dict にする |
| 43 | **`TestSearchedValuesDoNotEnterParams`** | `TrialAssignment.as_tags()` の内容を `TrainerConfig.as_params()` へ合流させる実装に変える（`hpo.study_name` を param 側へ移す） |
| 44 | `TestComposeThenSearch` | 上記のどれか（通し経路なので広く落ちる。単独の機構検証には使わない） |
| 45 | `test_architecture::TestDependencyFreeLayer` | `ml/tuning/study.py` に `import optuna` を足す |
| 46 | `test_architecture::TestRuntimeLayer` | `ml/training/loop.py` に `import optuna` を足す（`ml-runtime` 層の汚染検出） |
| 47 | `test_architecture::TestDomainIndependence` | `ml/config/composition.py` に `from pcbasm.config import Machine` を足す |

太字の 12 行（#12, #19, #23, #25, #30, #34, #35, #39, #40, #41, #43 と #45）は
**この MR の存在理由そのもの**なので、変異実験を必ず通すこと。

---

## 17. 規約チェックリスト（実装者向け）

- `src/ml/` から `pcbasm` / `web` を import しない（相対 import での迂回も不可）
- **ABC を増やさない。** 既存 3 つ（`ExperimentLogger` / `TrainingTask` /
  `TrainingData`）のみ。`TrialObjective` は ABC ではなく `Callable` の type alias
- **Protocol は使わない。** `@override` は ABC 実装にのみ付く
- `@attrs.frozen`。`@dataclass` は使わない（MR185 は `@dataclass(frozen=True)` だった）
- 検証は例外ではなく `validate() -> str | None`。`raise ValueError` を新規に増やさない
  （MR185 は全面的に `raise` していた）
- クラスに属する関数は module-level に置かず classmethod / メソッドにする。
  既存の型：`ModelSize.measure` / `SplitManifest.build` / `PaddedBatch.pad`。
  MR5 で増える型：`ConfigComposition.from_arguments` / `StudyIdentity.build` /
  `StudyResults.build` / `StudyResults.load` / `PackagedConfiguration.locate`
- 略語を綴りきる：`hyperparameter_search` / `learning_rate` / `configuration` /
  `optimizer` / `standard_deviation` / `experiment_run_id`。
  codespell の ignore-list で誤検知を抑えない
- docstring とエラーメッセージは日本語
- 内部実装と `__init__` で設定する属性は `_` prefix。テスト都合で public にしない
- `__all__` を module 末尾に置く（既存 module に揃える）
- `src/ml/config/__init__.py` と `src/ml/tuning/__init__.py` は **docstring のみ。
  re-export しない**（`src/ml/__init__.py` の方針に揃える）

### docformatter の落とし穴

- **docstring の説明部は 1 文 1 段落。** 1 文ごとに空行で区切る。
  `--wrap-descriptions=72` は文字数で折り返すので、1 文を 2 行に書くと
  句読点の直後を半角空白で継いで「落ち、 resume 拒否が」のような壊れた文になる
- **summary を小文字の識別子で始めない**（先頭が大文字化される）。
  `learning_rate を検証する` ではなく `設定の整合を検証する`
- **新規ファイルを含む変更では `git add -A` してから `make format` を走らせる。**
  pre-commit は git が知っているファイルだけを対象にするので、未追跡ファイルは
  `make format` が 2 回連続で pass しても**一度も検査されていない**
- `tests/ml/**` は `--doctest-modules` で collect される。docstring に `>>>` を書かない

### 検証

```bash
git add -A && make ml-docker-check   # format → ML の型検査 → tests/ml
```

`make ml-docker-check` を 2 回続けて走らせる（1 回目の format が書き換える）。
ベースラインは **592 passed / 1 skipped**。
**`make test` / `make run` / `pytest -m hardware` は絶対に実行しない。**

---

## 18. 想定リスク・トレードオフ

1. **`from_arguments` の group / 上書き判定がファイルシステムに依存する。**
   `configuration_root` にディレクトリが実在するかで意味が変わるので、
   同梱 conf の構成を変えると引数の意味が黙って変わりうる。
   代案（dotted 有無で判定）は `run_kind=finetune` を誤読するので採らなかった。
   緩和：`from_arguments` が group 選択と上書きの内訳を `ConfigComposition`
   のフィールドとして保持するので、呼び出し側がログへ出せる
2. **`ParameterDistribution` を tagged union にしなかった。**
   `kind` に対して無関係なフィールドが構造上は存在し続ける（`categorical` なのに
   `low` が書けてしまう）。`validate()` が理由を返すことで実行時には防ぐが、
   型レベルでは防げない。union の structure hook を converter へ足す案は
   決定 3（converter を変更しない）に反するため採らなかった
3. **`collect` の `experiment_run_ids` を呼び出し側が用意する。**
   `ml.tuning` が mlflow を import しない代償として、MLflow から run id を引く
   コードが `ml/` の外に出る。MR185 はこれを sweeper の中に埋めて 4 階層の
   委譲を招いた。外に出すのは意図的な選択で、`verify_lineage()` が事後に
   検査するので lineage の保証自体は落ちない
4. **並列の実証が「同一プロセス内の 2 インスタンス」で止まる。**
   真の 2 プロセス並列（`CUDA_VISIBLE_DEVICES` 分離）はテストしない。
   SQLite ファイル経由の合流経路は同じだが、GPU の取り合いや file lock 競合の
   実挙動は運用時に初めて出る。§3 で 2 プロセス並列は実測済みなので、
   MR5 では機構の検証に絞る
5. **同梱 `conf/trainer/edge.toml` の内容が装置寄りに見える。**
   `ml` はドメイン非依存なので、「edge（CPU・deadline 付き）」という profile 名が
   装置の匂いを持つ。`TrainerConfig` の `deadline_seconds` 自体は MR4 で
   ドメイン非依存に入っているので破綻はしないが、MR6 で paste_volume 側の conf を
   作るときに置き場所を再確認する
6. **`scripts/ml_smoke.py` の Hydra 参照（調査済み・解消手順あり）。**
   grep の結果、リポジトリ内の Hydra 参照は `scripts/ml_smoke.py` の 2 箇所だけだった
   （`_REPORTED_PACKAGES` の `"hydra-core"` と `report.skip("hydra compose", ...)`）。
   `src/`・`docker/`・`Makefile`・`.gitlab-ci.yml` には無い。
   commit 1 と commit 2 で処置する（§14・§20）。**この 2 箇所は
   `make ml-docker-check` では検出されない**（`ml_smoke.py` は pyright の対象だが
   pytest の対象ではない）ので、`make ml-docker-smoke` を別途走らせて確認する

---

## 19. 未決事項

1. **同梱 conf を MR5 で出荷するか。** 暫定案は `conf/trainer/edge.toml` 1 本のみ。
   §4 が「packaged config group（wheel 同梱）」をスコープに挙げており、
   wheel への同梱は実測で確認済み（§6）なので、機構を証明する最小の 1 本を置く。
   「MR5 では機構とテストだけにして同梱ファイルは MR6 の paste_volume 側で作る」
   という判断もありえる。その場合は `tests` が `tmp_path` の conf root を使い、
   `TestPackagedConfiguration` の同梱ファイル依存の観点（#14, #39）を MR6 へ移す
2. **`StudyResults` の成果物形式を JSON にした。** §4 は
   `optimization_results.yaml` と書いているが、決定 2（設定は TOML）と
   既存 `DocumentKind`（JSON、`atomic_write_json`）の二重化を避けるため
   **JSON（`hyperparameter_search_results.json`）**とした。
   外部ツールが YAML を要求するなら再考が必要
3. **`direction` を単一値にした。** optuna は多目的最適化（`directions`）を
   持つが、現状の要件は単一 metric なので `Direction` を 1 つだけ持つ。
   多目的が必要になったら `StudyResults.best_trial` の意味が変わるので、
   そのときに拡張する（今は「起こり得ないシナリオ向けの処理を増やさない」）
4. **`trial_count` の既定値 20。** optuna の既定に合わせた。実運用で
   2 GPU × 何 trial 回すかはユーザーの運用判断なので、既定値の妥当性は
   MR6 の実データで再確認する

---

## 20. `scripts/ml_smoke.py` の `hydra compose` 置き換え

MR5 は packaged config を実装するので、現在の
`report.skip("hydra compose", "packaged config (pcbasm.pasting.paste_volume.conf) は未実装")`
を実 check にできる。**skip のまま残すと「未実装」の表示が嘘になる。**

### 追加する check（commit 2）

```python
def check_packaged_configuration(report: Report) -> None:
    """同梱設定を合成して frozen attrs へ落とせる."""
```

- `main()` の `checks` tuple へ `check_packaged_configuration` を加え、
  `report.skip("hydra compose", ...)` の 3 行を削る
- 中身は `_checked(report, "packaged configuration")` の中で
  `PackagedConfiguration.locate()` → `ConfigComposition.from_arguments(
  ("trainer=edge",), configuration_root=..., base_names=())` →
  `structure(TrainerConfig, converter=make_strict_converter())` を通し、
  `report.ok` に group 数と option 名を出す
- 失敗は `AssertionError` で上げる（`_checked` が拾う既存の作法に合わせる）。
  `validate()` 由来の理由文字列はそのまま `AssertionError` のメッセージにする
- **check 名は `"hydra compose"` を引き継がず `"packaged configuration"` にする。**
  Hydra を使わないので名前を残す理由が無い

### 注意

`scripts/ml_smoke.py` は `src/ml/` ではないが `ML_TYPE_PATHS` に入っているので
`make type` の対象。ただし **pytest の対象ではない**（`testpaths = tests/`）ので、
`make ml-docker-check` では実行されない。**`make ml-docker-smoke` を別途走らせる。**

`ml_smoke.py` は `ml` を import するが、`src/ml/` から `scripts/` への依存は
発生しない（向きは `scripts` → `ml` の一方向）。`tests/ml/test_architecture.py`
の走査対象は `src/ml` と `tests/ml` なので、この check の追加で層の契約は動かない。
