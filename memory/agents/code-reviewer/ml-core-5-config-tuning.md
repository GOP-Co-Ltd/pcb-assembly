# MR5（ML 基盤の設定合成 + ハイパーパラメータ探索）レビュー

対象: `feature/2026-09-07/ml-core-5-config-tuning`（`git diff --cached main`、26 file）
参照: 計画 §13 / 裁定記録（決定 1〜3、裁定 1〜5）/ 実装ノート / テストノート

## verdict: request-changes

must-fix 3 件はいずれも実測で再現した。仕様準拠（§13 のシグネチャ）・層の分離・決定 3 の維持・
MR4 制約・テスト品質・変異実験の妥当性は問題なし。

## must-fix

### 1. `redacted_uri` が秘匿を保証しない経路が 2 つある

対象: `src/ml/tuning/study.py:126-133`（sqlite 分岐の理由文字列）、`:146-166`（`redacted_uri`）

- **`://` を含まない URI を素通しする。** `redacted_uri` は `partition("://")` で
    separator が無いと入力をそのまま返す。その結果 `validate()` の理由文字列に password が残る。

    ```
    StudyStorage(uri="postgresql:operator:secret@host/db").validate()
    → "storage URI に host がありません: 'postgresql:operator:secret@host/db'"   # secret が残る
    ```

- **sqlite 分岐だけ `redacted_uri` を使っていない。** `:128` と `:132` が `{self.uri!r}` を
    直接埋めている（server 分岐 `:137` / `:139` は `redacted_uri` を使っている）。

    ```
    StudyStorage(uri="sqlite:///operator:secret@rel.db").validate()
    → "sqlite storage は絶対パスが必要です（…）: 'sqlite:///operator:secret@rel.db'"
    ```

根拠: 計画 §13.3「`redacted_uri` は … **`validate` が通らない URI では呼ばない前提にせず、
`validate` 相当の判定を内部で通してから落とす**」の不履行。および `redacted_uri` 自身の
docstring「検証を通っていない URI でも呼べるようにする。失敗の理由文字列にも使うため、
例外を投げると秘匿できない経路ができる」に反する。
成果物（`StudyResults`）へは `build` が `validate` を先に通すので漏れないが、理由文字列は
呼び出し側がログへ出す前提の値。

テスト側の穴: `test_drops_every_place_a_credential_can_hide` / `test_never_writes_a_credential_into_the_document`
は `postgresql://…` 形（`://` あり）3 ケースだけ。変異 #19 / #58 もこの形しか通していないため
検出できていない。

確信度: 高（実測）。深刻度: 中（credential のログ流出）。

### 2. `integer` + `log=True` + `step>1` が validate を通り、探索が全滅しても `run` が成功を返す

対象: `src/ml/tuning/search_space.py:102-113`（`_validate_numeric`）、`:68-76`（`suggest`）、
`src/ml/tuning/runner.py:145-150`

`validate()` は `log` を `float`/`integer` 両方で許し、`step` を `integer` で許すが、
optuna はこの組み合わせを常に拒否する。

```
d = ParameterDistribution(kind="integer", low=1.0, high=100.0, log=True, step=2)
d.validate()                     → None（通る）
d.suggest(trial, name="x")       → ValueError: Samplers and other components in Optuna
                                    only accept step is 1 when `log` argument is True.
```

`suggest` は `HyperparameterSearch._evaluate` の中で呼ばれ、`study.optimize(..., catch=(Exception,))`
がこれを飲む。結果:

```
search.validate() → None
search.run(objective) → (StudyResults, None)   # error は None
   completed_trial_count = 0, states = ['FAIL', 'FAIL', 'FAIL']
```

根拠: §13.4「`validate` の内容：… `log` は `float` / `integer` のみ、`low > 0` を要求。
`step` は `integer` のみ」という契約は「suggest できる組み合わせを保証する」ためのもので、
`suggest` の docstring も「呼ぶ前に :meth:`validate` を通す前提」と宣言している。
`catch=(Exception,)`（§15.5 の要求）が objective の失敗と探索空間の構成ミスを区別できないため、
validate 側で塞ぐ以外に方法がない。optuna は警告をログへ出すので完全な沈黙ではないが、
API の戻り値は成功。

補足の提案: `trial_count >= 1` なのに `completed_trial_count == 0` なら理由を返す、という
ガードを `run` に置くと同種の穴を広く塞げる。

確信度: 高（実測）。深刻度: 中〜高（探索が 1 件も成立しないのに成功が返る）。

### 3. 既存 study と `direction` が食い違うと、成果物に誤った `best_trial` が載る

対象: `src/ml/tuning/runner.py:139-150`、`src/ml/tuning/study.py:263-275`

`StudyIdentity` は `direction` を含まないので、direction だけ違う 2 つの
`HyperparameterSearch` は同じ study 名を持つ。`optuna.create_study(load_if_exists=True)` は
既存 study の direction を黙って採用し（例外も警告も無し）、一方 `StudyResults.direction` と
`best_trial` はインスタンス側の `direction` で決まる。

実測（trial 0 = 0.9、trial 1 = 0.1、trial 2 = 0.5）:

```
1 個目: direction="minimize" で 2 trial → RDB の study.direction = MINIMIZE
2 個目: direction="maximize" で同じ storage / 同じ identity → run() は error=None
  results.direction = "maximize", results.best_trial = trial 0 (0.9)
  RDB 側の study.direction = MINIMIZE のまま、optuna の best_trial = trial 1 (0.1)
```

sampler は minimize として探索したのに、成果物は「maximize で best は最悪 trial」と記録する。

根拠: 裁定 2 が `identity.search_space_fingerprint != search_space.fingerprint` を
「空間の違う trial が 1 個の study へ合流する」ために塞いだのと同じ構造の穴。
study.py の module docstring も「空間の違う trial を混ぜると best trial の意味が変わる」を
この層の存在理由に挙げている。`create_study` の直後に `study.direction` と `self.direction` を
照合すれば 1 行で塞げる。

確信度: 高（機構は実測）。深刻度: 中（成果物が誤り。ただし発生には direction の明示変更が必要）。
**スコープに含めるかは orchestrator の裁定を仰ぐ**（「direction を identity に入れる」設計変更まで
広げるか、`run` での照合に留めるか）。

## should-fix

### 4. objective が `float("inf")` を返すと study が恒久的に読めなくなる

`src/ml/tuning/study.py:189-190`（`TrialRecord.validate` の非有限拒否）。optuna は inf を
COMPLETE として保存するため、`StudyResults.build` が理由を返し、**`run()` だけでなく以後の
`collect()` も永久に `(None, "trial 0 の value が非有限です: inf")`** になる（実測）。
HPO では「発散した設定に inf を返す」慣用があるので踏みうる。1 trial が study 全体の成果物経路を
潰す点が問題。確信度: 高（機構）/ 中（遭遇頻度）。

### 5. 境界を int で書いた探索空間と float で書いた探索空間の fingerprint が別になる

`ParameterDistribution.low/high` は `float | None` だが attrs は変換しないので、Python から
`low=2` と書けば int が入る。`fingerprint` は `attrs.asdict` → `canonical_json` なので
`2` と `2.0` で別ハッシュになる（実測: 別 fingerprint、どちらも `validate()` は None）。
同じ論理空間が別 study 名に分かれ、この MR の中心的な前提（同じ空間は 1 study へ合流）が崩れる。

さらに TOML からは int 形が書けない（`low = 2` は `expected float | None` で拒否）ので、
コードで書いた空間（`tests/ml/tuning/test_runner.py:56` は `low=2, high=10`）と TOML で書いた
同じ空間は必ず fingerprint が食い違う。`ParameterDistribution` の docstring にも
「integer でも境界は `2.0` と書く」旨が無く、TOML からの integer 分布のテストも無い。
確信度: 高（実測）。

### 6. 裁定 1 の fallback に、silent に誤った値が通る経路が残っている

`src/ml/config/composition.py:171-185`。TOML として壊れた token が raw 文字列として採用されるため、
到達先が `str` フィールドだと壊れた入力がそのまま値になる。

```
ConfigComposition(overrides=('monitor="loss',)).structure(T, ...)  → T(monitor='"loss')
```

型が違えば converter が拒否するので裁定 1 の想定は概ね成立しているが、`str` フィールドだけは
converter が守れない。`"` / `'` / `[` / `{` で始まる raw token は「裸の文字列」ではあり得ない
（意図された bare string に引用符は現れない）ので、そこだけ理由を返せば穴が閉じる。
確信度: 高（実測）。深刻度: 低〜中。

### 7. `docs/image-based-dispense-calibration-ml-plan.md` が Hydra 前提のまま残っている

AGENTS.md がこの文書を ML 実装の参照先に挙げているのに、この MR で消した依存をまだ規定している。
少なくとも次が実態と矛盾する。

- `:18` 「Hydraで設定を合成し、Optuna Sweeperでhyperparameterを探索する」
- `:38` / `:40` `ml-train` に `hydra-core`、`ml-hpo` に `hydra-optuna-sweeper`（両方削除済み）
- `:61` / `:68` 開発環境の確認項目（`ml_smoke.py` は `packaged configuration` に置き換え済み）
- `:817` `ml.config` の説明「Hydra境界とpackaged config group」

計画 §12 の変更ファイル一覧に docs が無いので実装者の逸脱ではないが、決定 1 を承認した文書と
正典が食い違ったまま merge される。確信度: 高（事実）。MR5 で直すか MR6 へ回すかは裁定事項。

### 8. `conf/trainer/edge.toml` が必須フィールドを持つため、profile を増やすと必ず重複する

`src/ml/config/conf/trainer/edge.toml:20-21`。`max_epochs` / `monitor` は既定値が無いので
「既定値の二重管理」ではない（実装ノートの説明は正しい）。ただし 2 本目の profile
（`trainer/gpu.toml` 等）を置くと両方がこの 2 行を書き直すことになり、MR185 の
「19 フィールドを両方が全部書き直していた」構造の入口になる。`conf/base.toml` に必須フィールドを
置き、group 層を純粋な差分にする（`base_names=("base",)`）方が §12 の意図に沿う。
`ml_smoke.py` の check も `base_names` を渡す形になる。確信度: 中（設計判断）。

### 9. `search_space.py` に新規の `raise ValueError` が 2 箇所ある

`:80`（categorical の None ガード）と `:117`（`_bounds`）。どちらも `validate()` を通していれば
到達しない防御分岐で、§17「`raise ValueError` を新規に増やさない」および裁定 5
（到達不能な `isinstance(loaded, dict)` を削除した判断）と一貫しない。`:80` は
`suggest_categorical` の戻り型（`CategoricalChoiceType`）を narrowing するために必要だが、
メッセージ「categorical の choices に None は書けません」は `choices: tuple[Scalar, ...]` が
None を許さないので実態と合っていない。確信度: 中（規約解釈）。深刻度: 低。

## nit

- `study.py:104-110` `study_name` は `_safe_name` の結果を strip しないので、`model_family="///abc"`
    が `-abc-…` になる（`validate` 側は `.strip("-")` で通す）。先頭 `-` は CLI 引数で扱いにくい。
- `study.py:257-260` 重複 trial 番号の検出が `numbers.count` の入れ子で O(n²)。共有 study が
    数千 trial まで伸びると効く。
- `runner.py:161-170` `collect()` は `KeyError` だけを捕まえる。storage ファイルが無い場合は
    optuna が空の SQLite を作ってから `KeyError` になるので、**読むだけの API が副作用でファイルを作る**
    （実測）。`create_study` 側（`:139`）は例外を一切包んでいないので、書けないパスでは
    理由文字列ではなく例外が飛ぶ。
- 裁定 1 の副作用として `str` フィールドへ TOML 解釈可能な文字列を渡せない（`monitor=1` は
    `expected str` で拒否。実測）。運用上の制約なので docstring に一行あると親切。
- `test_study.py:388-389` `assert "1" in error` / `"7" in error` は部分一致なので、trial 番号以外の
    数字でも通る（`[1, 7]` を丸ごと見る方が強い）。
- `DIRECTIONS` / `DISTRIBUTION_KINDS` は §13 に無い公開追加（実装ノートに記録済み・additive）。

## 問題が無いことを確認した観点

- **§13 のシグネチャは全 5 module で完全一致。** 引数名・keyword-only・戻り値型・既定値
    （`trial_count=20` / `direction="minimize"` / `base_names=()`）まで差分なし。
- **層の分離。** `ml.config.composition` / `ml.config.packaged` / `ml.tuning.study` は
    `DEPENDENCY_FREE_MODULES` に登録済みで、optuna を import しない。`ml.tuning.search_space` /
    `runner` だけが optuna を import し、どの登録リストにも入っていない。`src/ml` から
    `pcbasm` / `web` / `mlflow` への import は無い（`ml.tuning` は mlflow 非依存）。
- **`hydra` / `omegaconf` を forbidden から削った判断は妥当。** 当該 assertion は
    「install されていないので常に成立」で空虚だったうえ、仮に依存フリー層が `import hydra` を
    足しても subprocess が ImportError で落ち `check=True` により test は失敗する。検出力は落ちない。
    リストに残る 7 / 4 個はすべて実 install されている。
- **決定 3 は崩れていない。** `_exact_type` は無変更。union hook は `bool | int | float | str`
    という annotation にしか効かず、`float` / `float | None` は従来どおり int を拒否する
    （`learning_rate=1` → `expected float`、`low = 2` → `expected float | None` を実測）。
    `type(value) in _SCALAR_TYPES` が `IntEnum` を弾くのもテストで pin 済み。
- **MR4 の制約。** `_FINGERPRINT_EXCLUDED_FIELDS` の 4 フィールドは無変更で、
    `TestFingerprintClassificationSurvivesComposition` が合成経路越しに区別を pin している。
    `TrialAssignment.as_tags()` は `hpo.` 前置きの tag のみで、param との非交差もテスト済み。
- **テスト品質。** モックゼロ（optuna は in-memory study と `tmp_path` の実 SQLite、`Trial` も実物）。
    `src` の private を直接触るテストは無い。全て `class TestXxx` 集約。
- **変異実験。** 73 変異の対応表は実装と整合している。SURVIVED 5 件の補強はいずれも
    「別分岐が代替して観測結果が変わらない」を解消する方向で、#27a が `low` / `high` を埋めた値で
    書かれている点、#29 が `trial.distributions` を観測している点は正しい。補強しなかった 2 件
    （#8 の空キー、#32 との役割分担）の判断も妥当。ただし #19 / #58 の変異が `://` 形の URI しか
    通していないため must-fix 1 を取り逃している。
- **成果物汚染なし。** `</content>` 等の混入ゼロ、`殟` / `憅` の混入ゼロ（`src` / `tests` / `scripts`）。
    `make ml-docker-check` 後も worktree に未 stage の差分は無い。
- **wheel 同梱を実測確認。** `uv build --wheel` の中身に `ml/config/conf/trainer/edge.toml` が入る。
- 既定値の二重管理ガード（`TestDefaultsAreNotDuplicated`）は `edge.toml` の 5 行すべてに対して
    正しく機能する（`max_epochs` / `monitor` は既定値なしで比較対象外、他 3 行は既定と異なる値）。

## 検証結果

- make format（`make ml-docker-check` の pre-commit 全 hook）: **pass**（全 Passed / 2 Skipped）
- make type（`pyright src/ml tests/ml scripts/ml_smoke.py`）: **pass**（0 errors, 0 warnings）
- テスト（`pytest tests/ml -m "not hardware and not e2e"`）: **pass**（763 passed / 1 skipped）
- `make ml-docker-smoke`: **pass**（`packaged configuration  1 group（trainer=edge）`）
- `make test-no-hardware` はこの機体で collect 不能（pcbnew / picamera2 無し）。実機側はユーザー確認。

## ユーザーへの質問（orchestrator 中継）

1. **`direction` を study の同一性に含めるか**（must-fix 3）。`run` で既存 study との照合に留めるか、
    `StudyIdentity` に direction を入れて別 study にするかで、運用時の合流の粒度が変わる。
2. **objective が `float("inf")` を返す使い方を許すか**（should-fix 4）。許すなら
    `TrialRecord.validate` の非有限拒否を「COMPLETE trial の value は有限」に狭めるのではなく、
    inf を記録可能にする（fingerprint 用の `canonical_json` は通らないので value は別扱い）判断が必要。

---

# 2 巡目レビュー（差し戻し対応）

対象: 1 巡目からの変更 10 file（`docs/` 1 / `memory/` 1 / `src/ml/` 4 / `tests/ml/` 4）。
`git diff --cached main` 全体も読み直した。

## verdict: approve

must-fix 3 件・should-fix 4 件すべて解消を確認した。残る指摘は should-fix 3 件と nit のみで、
マージを阻害するものは無い。新規 should-fix は 3 件とも「安全側に壊れる」種類で、
MR5 に含めるか MR6 へ回すかは orchestrator の裁定でよい。

## 1 巡目の指摘の解消確認

| # | 指摘 | 判定 | 確認方法 |
| --- | --- | --- | --- |
| must-fix 1 | `redacted_uri` の秘匿漏れ | **解消** | `_split_uri` で `urlsplit` を排除。私が挙げた 2 経路に加え、実装が見つけた 3 経路目（`operator:secret@host://db` で username が漏れる）と不正 IPv6 の例外も塞がっている。`CREDENTIAL_URIS`（14 形）で `redacted_uri` / `validate` の両方を pin。変異 F1-a〜F1-f が全 KILLED |
| must-fix 2 | `integer`+`log`+`step>1` の silent 全滅 | **解消** | `_validate_numeric` に `log and step != 1` を追加（実測: `validate()` が理由を返す）。加えて `run` が `completed_trial_count == 0` で理由を返す二重の防御。変異 F2-a〜F2-c |
| must-fix 3 | direction 不一致で誤った best_trial | **解消** | `create_study` 直後に `study.direction.name.lower()` と照合し、trial を積まずに拒否。テストが「拒否側の trial が 1 つも積まれていない」ことまで見ている。変異 F3 |
| should-fix (a) | `inf` が study を恒久的に潰す | **解消** | `_evaluate` の `math.isfinite` → `NonFiniteTrialValueError`。`run` 経由で `inf` が COMPLETE になる経路が消え、テストが `collect()` の読み直しまで確認。変異 Sa |
| should-fix (b) | int / float 境界で fingerprint が分岐 | **解消** | `type(bound) is not float` で拒否。`test_integer_bounds_written_as_int_change_the_fingerprint` が「拒否する理由そのもの」を pin し、TOML から integer 分布を書く経路のテストも追加された。変異 Sb |
| should-fix (c) | 裁定 1 の silent path（`monitor="loss`） | **解消** | 引用符で始まる token を理由で返す。対照（`loss"x` は通す）と過剰拒否変異 Sd の両方がある |
| should-fix (d) | docs が Hydra 前提 | **解消**（下記の事実確認つき） | 依存グループ・smoke 項目・config 構成・Optuna 節・Phase 3・参照リンクまで一貫して更新済み |
| should-fix (e) | `edge.toml` の必須フィールド | 変更なし（**妥当**） | 「既定値の二重管理ではない」は正しく、`conf/base.toml` を今足すのは AGENTS.md 開発原則 2 に反する。2 本目の profile を足す MR で再検討する旨がノートに記録されている |
| should-fix (f) | 新規 `raise ValueError` 2 件 | **削除（妥当）** | どちらも `validate()` 経由なら到達しない分岐で、裁定 5 と同型。`cast` への置き換えで pyright 0 errors を維持。`suggest_categorical` の戻り型は実際に `None` を含むので `cast` は必要な最小手当て |

### docs の事実確認（重点依頼）

**創作は見つからなかった。** 「Hydra を外した理由」3 点は orchestrator 記録（決定 1）の事実に対応する。

- 「dev release を 3〜4 点同時に固定」← 決定 1「案 A は dev 固定 1 点ではなく 3〜4 点になる」
- 「安定版 sweeper は `optuna<3` を要求」← 同「安定版 sweeper 1.2.0 は 2022-05 で `optuna<3` を要求」
- 「multirun は `BasicLauncher` の逐次 `for` ループ」「真の並列には launcher plugin が要る」← 同
- 「既定値を attrs へ寄せる方針が `@package` / custom resolver / `job.num` 注入をほぼ不要に」← 同

実装との整合も確認した。`ml-hpo` = `ml-train` + `optuna` ✓、smoke 項目 6 の記述と
`check_packaged_configuration` の実装 ✓、`src/ml/config/conf/trainer/edge.toml` の tree ✓、
「identity と search space の fingerprint が食い違う run、既存 study と direction が食い違う run は拒否」✓、
「生の storage URI は成果物にも理由文字列にも書かない」✓、`optimization_results.yaml` →
`StudyResults` document ✓、`group=option` / `key=value` の振り分け規則 ✓。

### 新機構 14 変異の妥当性（重点依頼）

**妥当。新機構に 1 対 1 で対応しており、抜けは下記 2 点（いずれも軽微）だけ。**

- 対応済み: scheme fullmatch(F1-a) / `://` 判定(F1-b) / `@` の最後の砦(F1-c) / sqlite 分岐の理由文字列
    2 箇所(F1-d, F1-e) / `urlsplit` 復帰(F1-f) / log×step(F2-a) / 全滅ガード(F2-b) / 内訳(F2-c) /
    direction 照合(F3) / 非有限(Sa) / 境界の float 照合(Sb) / 引用符拒否(Sc) / 過剰拒否の対照(Sd)
- 変異していないが**テストが守れている**機構: `_split_uri` の query / fragment 除去（1 巡目 #58 の
    移設先。`?password=secret` / `#secret` のケースが落ちるので機構は維持されている）、
    userinfo 除去（`rpartition("@")`。`test_redacts_the_password` が守る）
- **抜け 1（nit）**: 全滅時の理由から `（{trial_count} 件を要求）` を削る変異は SURVIVE する。
    `test_reports_a_search_where_no_trial_completed` の `assert "3" in error` が
    先行する `"FAIL=3"` で既に満たされるため（実測ではなく文字列の包含関係から確定）
- **抜け 2（nit）**: `_split_uri` の `scheme.lower()` は変異もテストも無い（下記 should-fix 12 参照）

## should-fix（新規）

### 10. `validate()` が通るのに `redacted_uri` が識別情報を失う URI がある

対象: `src/ml/tuning/study.py:125-150`（`validate`）、`:161-185`（`redacted_uri`）、`:342-359`（`_split_uri`）

password に percent-encode されていない `/` を含む URI は、**SQLAlchemy が正常に解釈する
実用的な URI** なのに、成果物へ storage の識別情報が 1 文字も残らない。

```
StudyStorage(uri="postgresql://operator:sec/secret@db.example/hpo").validate()  → None
                                                     .redacted_uri  → "<解釈できない storage URI>"
StudyResults.build(...).storage_uri_redacted                        → "<解釈できない storage URI>"
```

（SQLAlchemy の実測: `make_url` は username=`operator` / password=`sec/secret` /
host=`db.example` / database=`hpo` と解釈する。password の `[^@]*` が `/` を許すため。）

`StudyResults.validate()` は「空でない」しか見ないので placeholder を通す。結果、
study.py の module docstring が掲げる「成果物には秘匿済みの storage URI しか載せない
（storage の識別に要らない部分は残さない）」のうち**識別できる部分まで消える**。

なお `_split_uri` の「`@` は最初の `/` より前にあるものだけ userinfo」という規則自体は
RFC 3986 に忠実（userinfo の `/` は percent-encode が必須）で、`sqlite:////data/a@b.db`
のような path 中の `@` を壊さないために必要。したがって解析側を変えるのではなく、
**`validate()` が `redacted_uri == _UNREADABLE_URI` を拒否する**（記録できない URI は
最初から受けない）のが最小の手当て。

確信度: 高（実測）。深刻度: 低〜中（漏洩ではなく、成果物の識別情報の欠落）。

### 11. 2 巡目の裁定が裁定記録に残っていない

`memory/agents/orchestrator/ml-core-5-config-tuning.md` は 1 巡目のまま（95 行、決定 1〜3 のみ）。
今回の裁定 —— must-fix 3 を `StudyIdentity` ではなく `run` で塞ぐ根拠（MR4 の
`resume_rejection` と同型。実物を確認: `src/ml/training/checkpoint.py:295`）、
should-fix (a) を `NonFiniteTrialValueError` にした根拠（MR4 の `NonFiniteLossError`。
実物を確認: `src/ml/training/loop.py:73`）、(e) を変更なしとした判断、(f) を削除とした判断、
**私の質問 2 への回答（`TrialRecord` の非有限拒否を維持する）** —— がどこにも記録されていない。

実装ノートは should-fix (a) の項に「レビューのユーザー質問 2 が未裁定」と書いたままで、
裁定が出た事実と矛盾する（MEMORY.md「重複・陳腐化したメモリは速やかに更新または削除する」）。
質問 2 の回答は「既知の制約として記録する」方針なのに、記録先が無い状態。

確信度: 高（ファイルを確認）。深刻度: 低（コードは正しい）。

### 12. `scheme.lower()` は optuna が使えない URI を通す

`_split_uri` が scheme を小文字化するため `validate()` が `SQLITE:////var/lib/x.db` /
`POSTGRESQL://host/db` を受ける。一方 `run` / `collect` は生の `self.uri` を optuna へ渡すので
SQLAlchemy が落ちる。

```
StudyStorage(uri="SQLITE:////var/lib/x.db").validate()  → None
optuna.create_study(storage="SQLITE:////tmp/upper.db")  → NoSuchModuleError:
    Can't load plugin: sqlalchemy.dialects:SQLITE       # 理由文字列ではなく例外
```

1 巡目の `urlsplit` も scheme を小文字化していたので新規の退行ではない。小文字化を
落とせば「未対応の scheme です」で拒否できる。テストも変異も無い唯一の新機構。

確信度: 高（実測）。深刻度: 低（入力が稀）。

## nit（新規）

- `runner.py:186-189` 全滅時の理由から `（{self.trial_count} 件を要求）` を削っても
    `test_reports_a_search_where_no_trial_completed` は通る（`assert "3" in error` が
    `"FAIL=3"` で満たされる）。要求数を見るなら `f"{trial_count} 件を要求"` を直接見る方が強い。
- `search_space.py:135-138` `_bounds` は `cast` 2 個だけの helper になった。呼び出し側 2 箇所で
    `cast` するか、`suggest` 内で 1 回にまとめる方が読みやすい（code-simplifier 案件）。
- `search_space.py:117-119` `type(bound) is not float` が先に効くので、後段の
    `float(self.low).is_integer()` の `float(...)` は不要になった。
- `study.py:125` `validate` は不正な port（`db.example:notaport`）を通す。以前も同じなので退行では
    ないが、`redacted_uri` は `postgresql://db.example:notaport/hpo` を返すので秘匿は保たれている。
- docs `:435` 「Hydra を外した理由は次の 3 点である」は、決定 1 の**筆頭根拠**
    （現在 pin されている `hydra-core 1.3.6` + `sweeper 1.4.0.dev9` が実測で壊れている）を落としている。
    創作ではないが、最も強い事実が消えている。
- docs `:481` 「TPE sampler、固定 seed、1 プロセスあたり `trial_count=30`」は、
    `HyperparameterSearch` に sampler / seed を渡す口が無く、`trial_count` の既定は 20。
    MR6 で `ml.tuning` 側の変更が必要になる約束なので、doc 側に「MR6 で追加」と書くか
    既定値の表記を実装に合わせる方がよい。
- docs `:68` 「同梱group を表示する」の半角空白が周辺の表記（`data/paste_volume.toml`は…）と不統一。

## 1 巡目の nit（未対応・据え置きで可）

`study_name` の先頭 `-`、`StudyResults.validate` の O(n²)、`collect()` が空 SQLite を作る副作用、
`str` フィールドへ TOML 解釈可能な文字列を渡せない制約、`verify_lineage` テストの部分一致。
いずれも nit のままで支障はない。`collect()` / `create_study` が optuna の例外を包まない点は、
多目的 study に対する `study.direction` が `RuntimeError` を投げる経路も含むが、
いずれも「我々のコードが作り得ない study」を読む場合に限られる。

## 検証結果（2 巡目・orchestrator の報告を独立に再現）

- make format（pre-commit 全 hook）: **pass**（全 Passed / 4 Skipped、書き換えゼロ）
- make type（`pyright src/ml tests/ml scripts/ml_smoke.py`）: **pass**（0 errors, 0 warnings）
- テスト（`pytest tests/ml -m "not hardware and not e2e"`）: **pass**（**832 passed / 1 skipped**）
- `</content>` / `殟` / `憅` の混入: `src/` `tests/` `scripts/` `docs/` で **なし**
- `make ml-docker-check` 実行後も worktree に未 stage の差分なし
- 公開 IF: §13 のシグネチャは 5 module すべて 1 巡目と同一。追加は
    `ml.tuning.runner.NonFiniteTrialValueError`（`__all__` に追加、additive）のみ
