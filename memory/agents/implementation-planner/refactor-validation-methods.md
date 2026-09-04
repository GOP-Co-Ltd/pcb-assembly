# 値オブジェクトの検証をクラスのメソッドへ移す

## 概要

`memory/feedback_validation_method.md` の規約（値オブジェクトの検証は `validate_xxx(value)` ではなく
そのクラスの `validate()` メソッド）に沿って、`src/` の module-level 関数を棚卸しした。
調査は `validate_*` 18 個に加え、AST 走査（第 1 引数がクラスの module-level 関数）で
`src/pcbasm/` `src/web/` 全体をさらった結果を含む。

**結論の先出し：候補は全体で約 60 関数あるが、今回やるべきは 3 つだけ。**
残りは規約と無関係な別種のリファクタで、1 MR に混ぜるべきではない。理由は「推奨する実施範囲」に書く。

## 公開インターフェース案

推奨スコープ（MR-A）で確定するシグネチャ。いずれも戻り値は規約どおり `str | None`、例外は投げない。

```
DatasetView.validate(self) -> str | None          # src/pcbasm/pasting/dataset/metadata.py
LineLayout.validate(self) -> str | None           # src/pcbasm/pasting/flowcalib/lines.py
BoardConfig.validate(self) -> str | None          # src/pcbasm/pasting/flowcalib/board/config.py
```

移動元の module-level 関数（`validate_view` / `validate_line_layout` / `validate_board_config`）は
削除する。後方互換の別名は残さない（`__init__.py` の再 export は存在しないため影響なし）。

## 移動候補の一覧表（MR-A: 推奨スコープ）

| 現在のシンボル / 場所 | 移動先クラス | 移動後シグネチャ | 呼び出し元 (src/tests/scripts) | 確信度 |
|---|---|---|---|---|
| `validate_view` `pasting/dataset/metadata.py:44` | `DatasetView` (同 `:38`) | `DatasetView.validate(self) -> str \| None` | 2 / 3 / 0 | 高 |
| `validate_line_layout` `pasting/flowcalib/lines.py:104` | `LineLayout` (同 `:27`) | `LineLayout.validate(self) -> str \| None` | 4 / 2 / 0 | 高 |
| `validate_board_config` `pasting/flowcalib/board/config.py:163` | `BoardConfig` (同 `:154`) | `BoardConfig.validate(self) -> str \| None` | 2 / 5 / 0 | 高 |

呼び出し元件数は定義行と import 行を除いた実呼び出し数。scripts は全件 0。

内訳:

- `validate_view` — src: `dataset/recorder.py:91`（+ import `:30`）。tests: `test_metadata.py:41,46,62`（+ import `:17`）
- `validate_line_layout` — src: `flowcalib/lines.py:153,224`, `web/api/jobs/pasting/dispense_calibration.py:228,379`（+ import `:27`）。tests: `test_lines.py:121,124`（+ import `:13`）。加えて `lines.py:33` の docstring が `:func:` で参照しており要更新
- `validate_board_config` — src: `board/config.py:346,535`（同一モジュール内のみ、web からの呼び出しなし）。tests: `test_config.py:97,134,207,223,282`（+ import `:17`）

### 参考：MR-A に含めない近接候補

| 現在のシンボル / 場所 | 移動先 | 呼び出し元 | 確信度 | 扱い |
|---|---|---|---|---|
| `validate_custom_pad` `board/config.py:271` | `CustomPadDraft` / `CustomPadSpec` | 2 / 0 / 0 | 中 | 後述の理由で今回は据え置き |

## 除外した関数の一覧と除外理由

### A. 値オブジェクトを持たない検証（独立スカラーの突き合わせ）— 規約が明示的に除外

| シンボル / 場所 | 理由 |
|---|---|
| `validate_crop_margins` `vision/crop.py:26` | `crop_margin_mm` と `mask_margin_mm` の 2 スカラー比較。持ち主クラスなし |
| `validate_paste_diameters` `pasting/toolhead_offset.py:173` | `diameter_min` / `diameter_max` の 2 スカラー比較 |
| `validate_field_names` `pasting/params.py:198` | 引数が `Iterable[str]` |
| `validate_dataset_run` `pasting/dataset/recorder.py:57` | 全引数 `object` 型の kwarg 4 個。装置を開く前の事前検証で、対象インスタンスがまだ存在しない |
| `validate_initial_purge` `pasting/initial_purge.py:130` | `amount_ul` + `pad_id` + `PadHierarchy` + `Sequence[Pad]` の突き合わせ。主対象が一意でない |

### B. `config.py` の 8 個 — フィールド単位の scalar validator（**移せない**）

`validate_audio_device` `:51` / `validate_audio_volume` `:58` / `validate_region_overlap` `:138` /
`validate_positive_number` `:145` / `validate_non_negative_number` `:152` /
`validate_positive_odd_integer` `:159` / `validate_paste_lift_height` `:171` /
`validate_probe_board_edge_margin` `:305`（各 src=2 呼び出し、tests=0、scripts=0）

いずれも **クラスインスタンスではなく単一スカラーを受ける**。呼び出し元は 2 系統だけ:

1. 所有クラスの `__attrs_post_init__`（`Audio` / `PadAlign` / `PasteDispenser` / `Probe`）
2. `src/web/api/config_store.py:171` の `_coerce` — `spec.key` で分岐し、**オブジェクトが構築される前に 1 フィールドだけ**検証する

2 のため、`Audio.validate()` 等に畳んでも scalar 版を消せない。規約を満たすには
`validate()` メソッドを**追加**したうえで scalar helper も残すことになり、コードは純増する。
`validate_positive_number` / `validate_non_negative_number` / `validate_positive_odd_integer` は
そもそも `(name, value)` を取る汎用ヘルパで、特定クラスに属さない。**全 8 個を除外**。

なお `config_store.py:171` の `_coerce` は 110 行超の `if spec.key == ...` 梯子という別の匂いがあるが、
これは検証の置き場所ではなく設定スキーマの表現の問題であり、本 MR のスコープ外。

### C. ファクトリ・パーサ（戻り値がクラスで引数がクラスでない）— スコープ外

`parse_metadata` `dataset/metadata.py:217` / `parse_board_document` `board/config.py:398` /
`parse_pad_catalog_id` `:587` / `parse_footprint_id` `pcb/footprint.py:89` /
`decode_board_settings` `pasting/persist.py:79` / `resolve_initial_purge` `pasting/initial_purge.py:33` /
`build_pad_hierarchy` `pcb/grouping.py:276` / `plan_paste_targets` `pasting/workflow.py:71` /
`build_fill_plan` `pasting/fill_path.py:57` ほか多数。`classmethod` 化は今回スコープ外。

### D. 層の逆転が起きるもの

| シンボル / 場所 | 逆転の内容 |
|---|---|
| `is_pad_enabled` `pasting/settings.py:182` | `Pad`(pcb) に移すと pcb → pasting |
| `descendant_override_summary` `pasting/settings.py:257` | `PadHierarchyNode`(pcb) に移すと pcb → pasting（※ `PasteSettingsModel` 側なら可。後述 MR-C 候補） |
| `_resolve_pad_by_id` `pasting/initial_purge.py:220` | pasting 固有の日本語エラー文を返すので `PadHierarchy`(pcb) へは移せない |
| `move_to_cap` `parking.py:25` | `XYZStage`(hal) と `NozzleCap`(config) をまたぎ、どちらでも逆転 |
| `render_label` `posctrl/render.py:31`, `draw_overlay` `vision/overlay.py:39` | `Image`(vision) に描画責務が入る |
| `resolved_settings` / `tree` / `overrides` / `pad_info` `web/api/routers/pasting_view.py:293,297,321,410`, `fetch_status` `routers/common.py:56` | pcbasm のクラス → pydantic 応答モデルへの変換。移すと pcbasm → web |
| `control_payload` `routers/common.py:90`, `control_changed_event` `control_api.py:23` | `LeaseInfo`(`web/api/control.py`) は「FastAPI 非依存」と docstring で明言済み。pydantic 依存を持ち込む |
| `plan_rate_sweep` / `plan_speed_sweep` `flowcalib/lines.py:134,204` | `CalibrationParams`(params.py) に移すと params → lines の import が増える（現在 `lines.py:19` に `TYPE_CHECKING` 循環回避ガードあり）。解消できる可能性はあるが要検証で、検証規約とは無関係 |

### E. 3rd-party 型が第 1 引数（メソッド追加不可）

shapely `Polygon`（`geometry/polygon.py` 全般、`pasting/fill_path.py`、`toolhead_offset.py:184`）、
pcbnew SWIG 型（`pcb/footprint.py:251` 以降、`pcb/kicad.py:259`）、
matplotlib `Axes`（`visualization/*`）、starlette `Request`（`web/ui/pages.py:68`）、
zeroconf `ServiceInfo`（`web/*/discovery.py`）、attrs `Attribute`（`web/api/attrs_models.py:32`）。

### F. FastAPI ハンドラ / DI — 署名がフレームワーク契約

`src/web/api/dependencies.py` の 13 関数、`identity.py:34`、各 router の `@router.*` 関数、
`register(JobCatalog)` 系 8 個（job 登録の規約署名）。

### G. 呼び出し側が過大になるもの（件数を添えて）

| シンボル / 場所 | 移動先 | 呼び出し元 | 除外理由 |
|---|---|---|---|
| `resolve_pad_settings` `pasting/settings.py:148` | `PasteSettingsModel` | src 6 / tests 約 22 | tests 側の書き換えが約 22 箇所。移動先が `PasteSettingsModel` と `PadHierarchy` の 2 択で一意でもない |
| `resolve_node_settings` `pasting/settings.py:160` | 同上 | src 1 / tests 約 11 | 同上 |
| `own_override_summary` `pasting/settings.py:248` | `LevelSetting` | src 2 / tests 2 | 引数が `LevelSetting \| None`。メソッド化すると呼び出し側 2 箇所で `None` 分岐を書くことになり、行数が増える |

### H. 規約と無関係な「メソッド化候補」— 別 MR（下の「推奨する実施範囲」参照）

検証ではないが第 1 引数がクラスで、メソッド化が妥当なものが約 40 件ある。主なもの:

- `web/api/routers/pasting_view.py` の `Loaded` 系 9 関数（`layer_pads` `:339`、`build_route` `:510` ほか。src 計 20 呼び出し、tests 0）
- `flowcalib/board/config.py` の値オブジェクト操作群 — `normalize_board_config` `:341`、`normalize_custom_pad_draft` `:325`、`board_document` `:376`、`normalized_board_document` `:385`、`default_custom_pad_name` `:546`
- `flowcalib/board/layout.py:279,289` の `_packing_area` / `_purge_keepout`（`BoardConfig` の派生値を src 内 8 箇所で再導出している）
- `web/api/jobs/posctrl.py` の `_board_corners` `:335` / `_orthogonality_points` `:538` / `_check_all_pads_correctable` `jobs/pasting/common.py:419` — pcbasm 側へ戻すべきドメインロジック（webui-thin-wrapper 方針）
- `pcb/grouping.py` へ寄せられる `_find_pad_by_id` `pasting/initial_purge.py:235`（現状 O(n) 走査だが `PadHierarchy` は索引辞書を保持済み）

**いずれも「検証をメソッドに」という今回の規約とは別の関心事**なので MR-A には入れない。

## 実装ステップ

MR-A のみ。1 コミット 1 関心事、モジュール単位で 3 コミットに割る。互いに独立なので並列実装も可能。

1. **`refactor(pasting): DatasetView に validate を移す`**
   - `metadata.py`: `validate_view` を削除し `DatasetView.validate()` を追加（`is_finite_number` は既 import）
   - `recorder.py:30` の import から `validate_view` を外し、`:91` を `view.validate()` に
   - `tests/pcbasm/pasting/dataset/test_metadata.py`: import と 3 呼び出しを差し替え（`TestDatasetView` クラスはそのまま）

2. **`refactor(pasting): LineLayout に validate を移す`**
   - `lines.py`: `validate_line_layout` を削除し `LineLayout.validate()` を追加。`:33` の docstring 参照を `:meth:` へ更新
   - `lines.py:153,224` を `layout.validate()` に
   - `web/api/jobs/pasting/dispense_calibration.py:27` の import を外し `:228,379` を差し替え
   - `tests/pcbasm/pasting/flowcalib/test_lines.py`: import と 2 呼び出しを差し替え

3. **`refactor(pasting): BoardConfig に validate を移す`**
   - `board/config.py`: `validate_board_config` を `BoardConfig.validate()` へ移動（本体 107 行をクラス内へ再インデント）
   - **外側の `isinstance(config, BoardConfig)` ガードは削除する**（メソッドでは到達不能）。フィールドの `isinstance` チェック（`board` / `purge_pad` / `custom_pads` / `patterns`）は残す
   - 本体が呼ぶ `validate_custom_pad` / `is_kicad_length` / `parse_pad_catalog_id` / `is_custom_pad_catalog_id` / `_MAX_CALIBRATION_PAD_COUNT` はすべて同一モジュールか下位層（`pcb.units` / `utils`）なので循環 import は発生しない
   - `:346`（`normalize_board_config`）と `:535` を `config.validate()` に
   - `tests/.../test_config.py`: import と 5 呼び出しを差し替え

検証は各コミットで `make format && make type && make test-no-hardware`。

## テスト観点

既存テストの機械的な差し替えが中心で、新規テストは 1 本だけ足りない。

- **正常系**: 既存の「妥当な値で `None` を返す」ケースをそのまま `.validate()` 呼び出しに置換
  （`test_metadata.py:41,46` / `test_lines.py:121` / `test_config.py:134,223`）
- **異常系**: 既存の「不正値でエラー文を返す」ケースを同様に置換
  （`test_metadata.py:62` の parametrize 3 ケース / `test_lines.py:124` / `test_config.py:97,207,282`）
- **エッジケース（新規 1 本）**: `BoardConfig.validate()` で外側 isinstance ガードを落とすため、
  **フィールドに不正な型が入った `BoardConfig` が引き続きエラー文を返す**ことを明示的に固定する。
  既存の `test_invalid_runtime_values_do_not_leak_exceptions`（`test_config.py:282`、`width_mm=True` 等）が
  これをカバーしているので、**同テストが移行後も緑であることを確認すれば足りる**。追加は不要と判断
- 3 関数とも public のままなので、`memory/feedback_no_private_test.md`（private を直接テストしない）に抵触しない。
  テストは公開メソッド経由の振る舞い検証として成立し続ける

## 想定リスク・トレードオフ

- **`BoardConfig.validate()` の isinstance ガード削除が唯一の振る舞い変更。**
  現状 `validate_board_config(非BoardConfig)` は `"基板設定の形式が不正です"` を返すが、
  メソッド化後は `AttributeError` になる。リポジトリ内の呼び出し元は `config.py:346,535` の 2 箇所と
  tests 5 箇所だけで、**すべて実 `BoardConfig` インスタンスを渡している**（`test_config.py` の不正系も
  `BoardConfig(board=BoardSpec(width_mm=True))` のようにフィールドだけを壊している）。
  外部から untyped な値が来る経路は `parse_board_document` `:398` だが、こちらは `:535` で
  `BoardConfig(...)` を明示構築してから検証しているため到達しない。実害なしと判断
- **`validate_board_config` の diff は 107 行あるが、ほぼ全てインデント変更**。レビュー時は
  `git diff -w` で実質差分がゼロに近いことを確認できる。逆に言うと、この 1 関数だけで MR-A の
  行数の 7 割を占める。分割コミットにしているのはこのため
- **`validate_custom_pad` を今回見送る判断。** 引数が `CustomPadDraft | CustomPadSpec` の union で、
  本体が `is_draft` で分岐している。メソッド化するなら両クラスに `validate()` を置き、共通部を
  private ヘルパへ切り出す形になるが、これは「関数の移動」ではなく**設計変更**であり、
  差分も振る舞いのリスクも MR-A の他 3 件と質が違う。`BoardConfig.validate()` が同一モジュールの
  `validate_custom_pad()` を呼び続ける形は一貫性をやや欠くが、許容範囲と判断した（確認事項 2 に記載）
- **`LineLayout` は attrs field validator でフィールド単体は既に検証済み**（`gt(0.0)` 等）。
  `validate()` は「線が領域に収まるか」という**フィールド横断の制約**だけを見る。役割が違うので併存でよい
- 却下した案: 後方互換のため module-level 関数を `.validate()` への薄い委譲として残す案。
  呼び出し元が全て社内かつ 20 箇所以下なので、二重の入口を残す利益がない

## 推奨する実施範囲

**確信度「高」の 3 件（MR-A）だけに絞ることを推奨する。**

### diff 規模の見積もり

| スコープ | 変更ファイル数 | 概算行数 | 内訳 |
|---|---|---|---|
| **MR-A（推奨）** | 8 | 約 165 行 | うち約 120 行は `validate_board_config` の再インデント。実質的な変更は約 45 行 |
| + `validate_custom_pad` | +0（同ファイル） | +約 70 行 | union 分解のため設計変更を伴う |
| H 節すべて（約 40 関数） | 約 30 | 1,500 行超 | ほぼ全モジュールに波及 |

### 自己批判（AGENTS.md 開発原則 3「必要な範囲だけ変更」に照らして）

**この MR は、放っておくと過剰リファクタになる典型的な形をしている。** 具体的に 3 点。

1. **規約自身が一括改名を禁じている。** `memory/feedback_validation_method.md` の最終箇条書きは
   「`src/pcbasm/` の既存 `validate_*` 関数は、その周辺を触る機会に合わせて移す。
   **この規約のためだけの一括改名はしない（AGENTS.md 開発原則 3）**」と明記している。
   「規約に合わせる MR」を立てること自体がこの一文と正面から衝突する。
   → **反論材料はある。** `git log` を見ると、MR-A の対象 3 ファイルは全て直近に触られている:
   `dataset/metadata.py` と `recorder.py` は 2026-09-03 の `45d2033`
   「refactor(pasting): validate dataset views, ...」で、`flowcalib/lines.py` と `board/config.py` は
   同日の MR4 `d5f70a7` で変更されている。今回の規約フィードバックはまさにこれらの MR から出たもので、
   「周辺を触る機会」の直後にあたる。**この 3 ファイルに限れば規約の但し書きと矛盾しない。**
   逆に、直近で触っていないモジュールまで広げると但し書き違反になる。

2. **調査で出てきた約 60 件を全部やるのは明確に過剰。** H 節の候補（`Loaded` 系 9 関数、
   `resolve_pad_settings` 系、web→pcbasm のドメイン移送）はどれも「検証をメソッドに」という
   今回の関心事ではない。同じ MR に入れると diff の各行をユーザー要求へトレースできなくなる。

3. **`config.py` の 8 個を無理に移すと純増になる**（B 節）。「規約に全部合わせる」という
   目的意識で進めると、ここでコードを増やす方向に判断を誤りやすい。移さないのが正解。

### 別 MR として切り出すことを提案するもの（今回はやらない）

判断材料として挙げるだけで、**着手は推奨しない**。ユーザーが別途優先度を決めるべきもの:

- **MR-B（小・独立して価値あり）**: `_move_to` の重複解消。`src/pcbasm/posctrl/tour.py:27` と
  `src/web/api/jobs/posctrl.py:346` に**同名・ほぼ同処理の関数が 2 つある**（web 版だけ `speed` 引数を持つ、
  という形で既に実装が乖離している）。`BoardCalibrationResult.move_to(self, machine_pt, speed=None)` へ
  統合すれば重複が消える。2 ファイル・約 20 行。これは規約ではなく重複という実害への対処
- **MR-C（中）**: `flowcalib/board/config.py` の値オブジェクト操作群
  （`normalize_board_config` → `BoardConfig.normalized()`、`board_document` → `to_document()`、
  `normalize_custom_pad_draft` / `default_custom_pad_name` → `CustomPadDraft` のメソッド、
  `layout.py` の `_packing_area` / `_purge_keepout` → `BoardConfig` の property）。
  1 モジュールに閉じて概念的にまとまっており、8 箇所の重複導出も消える。ただし tests 約 11 箇所の書き換えを伴う
- **MR-D（大・非推奨）**: `web/api/routers/pasting_view.py` の `Loaded` 系 9 関数。
  依存逆転リスクはなく tests 影響も 0 だが、純粋なスタイル変更で src 20 箇所に波及する

## 実施順序

MR-A の 3 コミットは互いに独立（別モジュール・別テストファイル）なので、**並列実装が可能**。
`spec-test-author` を使う場合も、公開インターフェースは上記 3 シグネチャで確定しているため
テスト作成と実装を並行できる。

1. `dataset/metadata.py` 系（最小・独立）
2. `flowcalib/lines.py` 系（`src/web` に 1 ファイル波及）
3. `flowcalib/board/config.py` 系（最大・ただし大半が再インデント）

同一ファイルを触るコミットはないため、並列 agent の書き込み範囲は自然に分離される。
`board/config.py` だけは `validate_custom_pad` を残す関係で 1 と 2 より判断が要る。最後に置く。

## リスク

- **既存テストの旧関数名 import**: 3 テストファイル（`test_metadata.py:17`、`test_lines.py:13`、
  `test_config.py:17`）が module-level 関数を直接 import している。合計 10 呼び出しの機械的置換。
  3 関数とも public → public メソッドへの移動なので、`memory/feedback_no_private_test.md` の
  「private を直接テストしない」規約には**抵触しない**。テストクラス構成（`TestDatasetView` 等）も維持できる
- **`data/testing/schemas/` の on-disk 契約**: `paste_dataset_metadata_v1.json` は
  `to_dict` / `parse_metadata` によるシリアライズ形状をピンしている。`validate_view` は
  シリアライズに一切関与しないため**影響なし**。`test_metadata.py:236`
  `test_fixture_file_is_current_schema` と `test_recorder.py` のリポジトリ資産テストも変更不要
- **`scripts/migrate_codex.py` / `.codex/` への影響: なし。** `migrate_codex.py` は
  `.claude/settings.json` の `Bash(...)` permission だけを `.codex/rules/default.rules` へ変換する
  （同ファイル docstring）。`grep -rn "validate_" scripts/ .codex/` は 0 件。`make migrate-codex-check` は無関係
- **`__init__.py` の再 export: なし。** `grep -rn "validate_" --include="__init__.py" src/` は 0 件のため、
  パッケージ公開 API の変更は発生しない
- **循環 import: 発生しない。** 3 クラスとも検証本体が参照するのは同一モジュール内のシンボルか
  `pcbasm.utils` / `pcbasm.pcb.units` という下位層のみ（`board/config.py:1-18` の import で確認済み）
- **`validate` という名前の衝突: なし。** `grep -rn "def validate\b\|\.validate(" src/ tests/` は現状 0 件。
  attrs の `validator=` フィールド引数とも名前空間が別

## 確認事項

1. **そもそもこの MR を出すべきか。** 規約ファイル自身が「この規約のためだけの一括改名はしない」と
   書いている。対象 3 ファイルは直近 MR4/MR5 で触ったばかりなので「周辺を触る機会」と解釈できる、
   というのが暫定案（＝ MR-A を実施する）だが、規約の但し書きを厳格に取るなら
   「次にそのファイルを機能変更で触るときまで待つ」も筋が通る。どちらを取るか。

2. **`validate_custom_pad` を MR-A に含めるか。** 暫定案は**含めない**（union 型の分解は
   関数移動ではなく設計変更のため）。その結果 `BoardConfig.validate()` が同一モジュールの
   module-level `validate_custom_pad()` を呼ぶ形が残る。この中途半端さを許容するか、
   両クラスに `validate()` を置いて共通部を private ヘルパへ切り出すところまでやるか。

3. **MR-B（`_move_to` の重複解消）を今回まとめてやるか。**
   `posctrl/tour.py:27` と `web/api/jobs/posctrl.py:346` の重複は検証規約とは無関係だが、
   実装が既に乖離している実害のある重複で、規模も小さい（2 ファイル・約 20 行）。
   暫定案は**別 MR に分ける**（1 MR 1 関心事）。ただし「ついでに直す」判断もあり得る。

## 参照

- 規約: `memory/feedback_validation_method.md`、`memory/feedback_no_try_catch.md`（戻り値バリデーション）、
  `memory/feedback_no_private_test.md`（private を直接テストしない）
- 開発原則: `AGENTS.md` の「3. 必要な範囲だけ変更」
- skill: `refactor-conventions`（実装・テスト規約）、`testing-strategy`（テスト方針）、
  `webui-thin-wrapper`（H 節の web→pcbasm 移送候補の根拠）
- 対象コード: `src/pcbasm/pasting/dataset/metadata.py:38,44`、
  `src/pcbasm/pasting/flowcalib/lines.py:27,104`、
  `src/pcbasm/pasting/flowcalib/board/config.py:154,163,271`
- 除外根拠の実物: `src/web/api/config_store.py:171`（`_coerce` のフィールド単位検証）
