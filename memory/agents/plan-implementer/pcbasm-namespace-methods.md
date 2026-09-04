# pcbasm / web の module-level 関数をクラスのメソッドへ移す

対象は `src/pcbasm/` と `src/web/`（`src/ml/` は別 agent）。
先行 MR !195（検証のメソッド化）の一般化として、所有者クラスが一意な
module-level 関数を instance method / classmethod へ移した。

## 洗い出しの方法

`ast` で `src/pcbasm/` `src/web/` の全 module-level 関数を走査し、

- 第 1 引数の型注釈がリポジトリ内クラス → instance method 候補（256 件中 100 件超）
- 戻り値の型注釈がリポジトリ内クラスで第 1 引数がクラスでない → classmethod 候補

を機械抽出したうえで、同一モジュール内 / 層の逆転 / 呼び出し元件数で絞った。
走査スクリプトは scratchpad（`survey.py`）に置いたもので、リポジトリには残していない。

## 実装した分（確信度「高」のみ）

| commit | 移動 | 呼び出し元 (src/tests) |
|---|---|---|
| 7aa4d05 | `normalize_board_config` → `BoardConfig.normalized` | 2 / 5 |
| 7aa4d05 | `normalized_board_document` → `BoardConfig.to_document` | 1 / 0 |
| 7aa4d05 | `board_document` → `BoardConfig.to_normalized_document` | 0 / 5 |
| 7aa4d05 | `normalize_custom_pad_draft` → `CustomPadDraft.normalized` | 1 / 0 |
| 7aa4d05 | `default_custom_pad_name` → `CustomPadDraft.default_name` | 1 / 0 |
| 7aa4d05 | `custom_pad_shape_option` → `CustomPadShape.for_id` | 1 / 0 |
| 53b14ec | `estimate_mass_flow` → `MassFlowEstimate.estimate` | 1 / 6 |
| 53b14ec | `rotations_per_ul_round` → `RotationsPerUlRound.evaluate` | 1 / 1 |
| 9b302a6 | `footprint_envelope` → `FootprintEnvelope.measure` | 3 / 3 |
| 3712225 | `find_orphans` → `PasteSettingsModel.find_orphans` | 1 / 2 |
| 52454ba | テスト名の追随（`TestEstimateMassFlow` → `TestMassFlowEstimate` 等） | — |

## 計画外の判断ログ

- **`board_document` / `normalized_board_document` の名前**。元の 2 つは
  「`board_document` は正規化してから直列化」「`normalized_board_document` は
  正規化済み前提でそのまま直列化」という関係で、名前が実装と逆に読める。
  クラス名の接頭辞 `board_` を落とすと `document()` / `normalized_document()` になり
  意味の取り違えが起きるため、**動作に忠実な名前**へ寄せた
  （`to_document()` = そのまま直列化、`to_normalized_document()` = 正規化してから直列化）。
  振る舞い・戻り値は不変。
- **`flow.py` の戻り値注釈**。`from __future__ import annotations` の追加は attrs の
  型解決に影響しうるので避け、classmethod の戻り値は `typing.Self` にした。
- **`CustomPadShape.for_id`** は `_CUSTOM_PAD_SHAPE_BY_ID` の辞書引きだけの 4 行。
  「その型を返すファクトリ」の条件は満たすが、単体では価値が小さい。
  同一モジュールの CustomPad 系をまとめて移す commit に同梱した。
- **`BoardConfigError` の docstring** の `` `normalize_*` の invariant `` を
  `` `normalized()` の invariant `` に更新（消えたシンボル名を指したままにしないため）。
- **`loading_controls.js:185` のコメント**にも `estimate_mass_flow` があったので更新した。

## 実装しなかったもの（確信度 中 / 低）と理由

orchestrator の判断待ち。詳細は最終報告に載せた一覧を参照。要点のみ:

- **`PadHierarchy.build`（`build_pad_hierarchy`, src 9 / tests 40）** — 確信度は高いが、
  本体 65 行の移動 + 49 箇所の書き換えで単独 +230 行。600 行上限に触れるため見送り
- **`FillPlan.build` / `FillPlan.for_pad`（`build_fill_plan` src 4 / tests 24、
  `build_pad_fill_plan` src 6 / tests 11）** — 同上
- **`GCode` の 7 ファクトリ（`homing` / `move` / `wait` / `wait_for_done` /
  `present` / `firmware_restart` / `relax`）** — `wait_for_done` だけで src 35 箇所
- **`web/api/routers/pasting_view.py` の `Loaded` 系 9 関数** — 依存逆転はないが
  純粋なスタイル変更で src 20 箇所に波及（先行調査でも「非推奨」判定）
- **`flowcalib/board/layout.py` の `_packing_area` / `_purge_keepout`** — `BoardConfig` の
  派生値だが、移すと `config.py` が `geometry.packing.Rect` に依存する。判断が要るので保留
- **層の逆転・3rd-party 型・FastAPI 契約・スカラー検証** — 先行調査
  `implementation-planner/refactor-validation-methods.md` の D〜G 節の除外判断をそのまま踏襲

## 他 implementer への IF 変更通知

`src/ml/` を担当する agent とはファイルが重ならないので影響なし。
`src/web/` 側で触ったのは `board_settings.py` / `jobs/pasting/dispense_calibration.py` /
`routers/pasting_loading.py` / `static/js/loading_controls.js` の 4 ファイルのみ。

## 既知の制約・残課題

- 旧名の `grep -rn` は src / tests / scripts / docs / data すべてで 0 件。
  `memory/agents/implementation-planner/*.md` には旧名が残るが、過去の調査記録なので更新しない
- `tests/pcbasm/pcb/test_footprint.py:123`
  `test_smd_pad_footprint_envelope_matches_size` は残した。
  `smd_pad_footprint` が現存する関数名で、消えた `footprint_envelope` を指しているわけではない

## 検証結果

- make format: pass（2 回連続で無変更）
- make type: pass（0 errors）
- make test-no-hardware: pass（2758 passed / 140 deselected。件数は main と同じ）
- 総 diff: 17 files, +260 / -275（`git diff -w` では +203 / -218。差は再インデント分）
