# MR5（設定合成 + ハイパーパラメータ探索）裁定記録

引き継ぎ資料は `memory/agents/orchestrator/ml-core-5-handoff.md`（MR !203、`main` 未 merge）。
作業ブランチは `feature/2026-09-07/ml-core-5-config-tuning`（`main` = 56f6fab から分岐）。

実行機体は GPU ワークステーション（RTX 4090 + RTX 4060）。pcbnew / picamera2 が無いため
`make test-no-hardware` は collect できない。検証は `make ml-docker-check`。
ベースラインは 592 passed / 1 skipped。

## 決定 1: 案 C（Hydra なし）を採用

引き継ぎ資料 §3 の 3 案から案 C を選んだ。ユーザーが承認済み。

要件は「並列 run」と「config 上書き」であって Hydra は手段の候補にすぎない、という前提で
`implementation-planner` に実測させた。決め手は次の 4 点。

**現在 commit されている `ml-hpo` の組み合わせは壊れている。** `uv.lock` 由来の
hydra-core 1.3.6 + hydra-optuna-sweeper 1.4.0.dev9 で最小 sweep を回すと
`InstantiationException('Cannot instantiate config of type TPESampler')` で即落ちする。
原因は sweeper dev9 が `instantiate(sampler, _execution_whitelist_=...)` を呼ぶこと。
この引数は hydra-core 1.3.6 に存在しない。

（メインエージェントが独立に確認: `_execution_whitelist_` の grep は
`hydra_plugins/hydra_optuna_sweeper/` に 1 件、`hydra/` に 0 件。）

**案 A は dev 固定 1 点ではなく 3〜4 点になる。** 動く構成は
sweeper 1.4.0.dev9 + hydra-core 1.4.0.dev9 + omegaconf 2.4.0.dev15。
dev9 同士は lockstep で upload 差 3 秒、cadence は 2 週間。安定版 sweeper 1.2.0 は
2022-05 で `optuna<3` を要求する。hydra-core 1.4.0 系は 2024-07 から 2 年以上 dev のまま。
yank の履歴はゼロなので、リスクは yank ではなく churn と解除時期の不明さ。

**Hydra の multirun は並列化しない。** `BasicLauncher.launch` は単一プロセスの素の
`for` ループ。`n_jobs` は Optuna の `ask` をバッチ化するだけ。真の並列には launcher plugin
（安定版は 2022 年、1.4 系は dev9）が要り、dev 固定が 4 点目になる。

（メインエージェントが独立に確認: `inspect.getsource(BasicLauncher.launch)` は
`for idx, overrides in enumerate(job_overrides)` の逐次ループ。）

**要件を満たしているのは Optuna 単体だった。** 2 プロセスが 1 つの SQLite study を共有して
10 trial を 2.12 秒（逐次 3.58 秒）。3 番目のプロセスが `load_if_exists=True` で 14 trial まで
resume。つまり「並列 run」に対する Hydra の寄与はゼロ。

**案 C の自作量は実測 103 行。** planner が実物を書いて本物の `ml.serialization` に通し、
6 ケース（既定値のみ／TOML 差分 2 層 merge／CLI dotted 上書き／未知キー拒否／型違い拒否／
壊れた上書き）で検証した。案 B との差は合成層 1 モジュールだけで、代わりに
hydra-core / omegaconf / antlr4 が依存から消える。

引き継ぎ資料 §4 の「既定値は attrs にのみ置き、TOML は差分だけ書く」方針が、
config group・`@package`・custom resolver・`job.num` 注入をほぼ不要にしている点が効いた。
MR185 の `trainer/gpu.yaml` と `pi.yaml` は 19 フィールドを両方が全部書き直していて、
実際の差は 8 個だけだった。

### 却下した案を採るべき条件

- **案 B（Hydra で合成のみ）** — 合成層の自作が「車輪の再発明」と判断された場合。
    境界（`plain dict → strict cattrs → frozen attrs`）は案 C と同一なので、
    差し替えは合成層 1 モジュールで済む
- **案 A（stock sweeper）** — hydra-core 1.4.0 と hydra-optuna-sweeper 1.4.0 が
    両方安定版になり、かつ並列 launcher も安定版が出た場合。
    それでも `BasicLauncher` が逐次である事実は変わらないので、並列化の主体は Optuna のまま

### MR185 の失敗から避けること（引き継ぎ資料 §3）

`hydra_plugins...OptunaSweeperImpl` の private 継承と 4 階層の委譲ラッパーは再現しない。
案 C では sweeper plugin そのものが無いので、この失敗様式は構造的に起こらない。

代替表のうち後半 2 点（`load_if_exists` に resume を任せる、lineage は事後の純関数で検証）は
案 C でもそのまま成立する。前半 2 点（study 名と checkpoint dir を OmegaConf resolver で解決）は
resolver が無くなるので、素の Python 純関数になる。
planner の指摘どおり、MR185 の study 名 fingerprint は `resolve_dataset_inputs` で
データセットを実際に読んでいた。resolver に I/O を持ち込む形は案 A / B の方が無理があった。

## 決定 2: 設定ファイルは TOML

stdlib `tomllib` で読めるので新規依存ゼロ。`src/pcbasm/config.py` + `machine.toml` の
前例と揃う。PyYAML だと `ml-runtime` だけの Raspberry Pi 5 に依存を足すことになる。

## 決定 3: strict converter は変更しない

`ml/serialization.py` の `_exact_type` は `bool` を `int` として通さないための機構で、
副作用として `learning_rate = 1` も「float が必要です」で拒否する。

converter を緩めず、**「float フィールドは小数点か指数を必ず書く」を規約**にし、
`learning_rate = 1` の拒否をテストで pin する。

合成層で attrs のフィールド型を見て int→float 昇格させる案は却下した。
`bool` が `int` の部分型である以上、昇格側にも除外ロジックが要り、
「記録した値と実際に使われた値が食い違わない」という converter の意図を薄める。

## 副産物（planner の確認）

- **wheel 同梱は追加設定不要。** `uv build --wheel` の中身に `src/` の非 `.py` が
    全部入っている（html 33 / js 20 / wav 2 / css 1 / typed 1）
- `tests/ml/test_architecture.py` の `"hydra"` / `"omegaconf"` を参照する assertion は
    案 C では install されないため空虚になる。MR5 で処置する

## レビュー 1 巡目の裁定（must-fix 3 件・should-fix 6 件）

`code-reviewer` の verdict は **request-changes**。must-fix 3 件は orchestrator 側でも
独立に再現した。詳細は `memory/agents/code-reviewer/ml-core-5-config-tuning.md`。

### must-fix 1: `redacted_uri` の秘匿漏れ

`://` を含まない URI が素通しになり、理由文字列に credential が残っていた。
sqlite 分岐だけが理由文字列に生 URI を埋めていた。

実装側が調査中に、指摘より広い欠陥を 2 つ見つけた。

- `urlsplit("postgresql://operator:secret@[::1/hpo")` が `ValueError: Invalid IPv6 URL`
    を投げる。つまり `validate()` は秘匿漏れ以前に「失敗は例外ではなく理由文字列で返す」
    約束を破る経路を持っていた
- `operator:secret@host://db`（userinfo が scheme の位置へ来る形）で `urlsplit` が
    `operator` を scheme として受け、`未対応の storage scheme です: 'operator'` で
    username を漏らしていた

`StudyStorage` から `urlsplit` を完全に外し、private helper `_split_uri` を置いた。
orchestrator が 5 経路すべて漏れなしを実測確認した。

### must-fix 2: `integer` + `log=True` + `step > 1`

`validate()` を通るのに optuna が `suggest` で拒否するため探索が全滅し、
それでも `run` が `(StudyResults, None)` で成功を返していた。

`validate()` で塞ぎ、あわせて `run` は完走 0 件なら理由を返すようにした
（要求 trial 数と state 内訳を載せる）。

### must-fix 3: `direction` 不一致で成果物に誤った best が載る

**裁定: `StudyIdentity` に `direction` を足さず、`run` が既存 study との不一致を拒否する。**

orchestrator の実測では、minimize で作った study に maximize で合流すると
`run` は error を返さず、成果物が全 trial 中の最大値を best と記録した。
実 study の direction は MINIMIZE なので、記録された best は実際には最悪の trial。

識別子に足す案を却下した理由は 2 つ。§13 の公開シグネチャと study 名の形が変わること。
direction を変えた利用者が黙って別 study に分かれるだけで何が起きたか伝わらないこと。

採った案の根拠は MR4 の `TrainingCheckpoint.resume_rejection()`。
「永続化された状態が現在の設定と合わなければ理由を返す」という型が既に確立している。

### should-fix の裁定

- **(a) objective の非有限値**: 失敗として扱う。`NonFiniteTrialValueError`
    （`RuntimeError` 派生）を投げ、optuna の `catch` が FAILED にする。根拠は MR4 の
    「再試行しても直らない非有限 loss だけを失敗として扱う」と `NonFiniteLossError` の前例
- **(b) `low=2` と `low=2.0` で fingerprint が別になる**: `validate()` が
    `type(bound) is not float` を拒否する。決定 3 と `_exact_type` の思想に揃える
- **(c) 裁定 1 の fallback で `monitor="loss` が silent に通る**: raw が引用符で始まって
    TOML 解釈に失敗したら理由を返す。裸の文字列は引き続き通す
- **(d) `docs/image-based-dispense-calibration-ml-plan.md` が削除済み依存を規定**: 案 C の
    実態へ同期させた。初稿で Hydra を外した理由を一部創作していたため、本記録の決定 1 の
    事実へ書き換えさせた
- **(e) `edge.toml` の必須フィールド重複**: 変更しない。`max_epochs` / `monitor` は
    既定値を持たないので「既定値の二重管理」ではない。base 層は要求されていない機構。
    2 本目の profile を足す MR で再検討する
- **(f) `search_space.py` の `raise ValueError` 2 箇所**: 実装側の判断で削った。
    どちらも `validate()` を通れば到達しない防御分岐で、裁定 5 と同型。
    narrowing は `cast` で済ませ pyright 0 errors を維持

### レビュアーの質問 2 への回答: 非有限拒否を維持する

`TrialRecord.validate` の非有限拒否は**変更しない**。

`run` 経由では `inf` が COMPLETE として記録され得なくなった。残る経路は外部ツールが
作った study を `collect()` で読む場合だけで、これは我々のコードが作り得ない。

再解釈機構を足すのは AGENTS.md の「起こり得ないシナリオ向けの処理を増やさない」に反する。
理由文字列は `trial 3 の value が非有限です: inf` で trial 番号と値を含み、診断に足る
（orchestrator が実測確認）。**既知の制約として記録する。**

## レビュー 2 巡目: approve

1 巡目の指摘は全件解消。docs の事実確認でも創作は見つからなかった。
新機構に対する変異実験 14 件は survivor 0 件。

新規 should-fix 3 件のうち 2 件を修正対象とした。

- **#10**: `validate()` が通るのに `redacted_uri` が placeholder に落ちる URI がある
    （password に percent-encode されていない `/` を含む形。SQLAlchemy は正常に解釈する）。
    成果物から storage の識別情報が失われるので `validate()` で拒否し、
    理由文字列で percent-encode を促す
- **#12**: `scheme.lower()` により `SQLITE:////...` が通り、optuna では
    `NoSuchModuleError` になる。退行ではないがテストも変異も無い唯一の新機構だったので拒否する
- **#11**: 本記録が 1 巡目のままだった件。この節がその対応

## 運用上の落とし穴（このセッションで踏んだもの）

### docformatter がファイルを書き換えるとき無関係な日本語文字が化ける

引き継ぎ資料が記録していた「1 文を 2 行で書くと句読点の直後が半角空白で継がれる」とは
**別の壊れ方**を発見した。

```
実 U+5B9F → 殟 U+6B5F
内 U+5185 → 憅 U+61C5
```

切り分けの結果:

- 該当 docstring だけを probe に入れても再現しない（docformatter は何も変更せず Passed）
- 実ファイルでは再現する。引き金は**別の行**の summary が `--wrap-summaries=79` を超えて
    折り返され、**ファイル書き換えが発生すること**
- その summary を短くして書き換えを起こさなくすると、文字化けは発生しない

**防御は「docformatter に書き換えさせない」ことのみ。** 根本原因は未解明。
docstring を足したら `--check` が exit 0（書き換えゼロ）であることを確認する運用にした。

折り返し判定は表示幅ではなく `len()`（`spec-test-author` が実測。既存 HEAD 済みファイルに
表示幅 88 の docstring 行が残っているため、CJK を幅 2 で数える説は成り立たない）。

小文字の識別子で summary を始めると先頭が大文字化される件は引き継ぎ資料どおりで、
このセッションでも 4 件実例を踏んだ（`userinfo だけでなく…` → `Userinfo …` など）。

### 変異実験の安全手順は orchestrator が `git add` すると崩れる

index を正本にして `git restore --worktree` で変異を戻す手順を採ったが、
orchestrator が検証のため `git add -A` を実行したラウンドでは
「index が前ラウンド終了時点」になり、`git restore --worktree` が修正そのものを
巻き戻す状態になった。

実装側は scratchpad への snapshot + sha256 一致検証へ切り替えて drift ゼロを保った。
**変異実験を委譲している間、orchestrator は index を触らない。**
