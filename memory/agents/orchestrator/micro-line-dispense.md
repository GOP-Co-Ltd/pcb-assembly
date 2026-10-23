# 微小線塗布（micro_line）

ブランチ: `feature/2026-10-23/micro-line-dispense`

## 要件（ユーザー合意 2026-10-23）

- 線塗布の微小版。上空で塗布量 V を先に押し出して静定を待ち、ノズル先端のドームを基板へこすりつける
- 背景: 0.25 x 0.88 mm パッド・0.3 mm ノズル・`ul_per_mm2 = 0.05`（V ≈ 0.011 μL）で、
  吐出の立ち上がり遅れ（機体ごとに一定、日によって変わる）と移動の開始がずれ、始点欠け・終点過多が出る
- Z は始点高さ `z_start` → 終点高さ `z_end` の線形。当面は `z_start == z_end`（水平）で使う
- リトラクトは上昇後（ノズル端面への持ち越し対策）
- 端の inset は micro_line のときだけ外径基準（`boundary_margin + nozzle_outer_diameter / 2`）
- 走行速度は `max_fill_speed` を流用
- auto: line に決まった pad のうち、短辺がノズル内径以下なら micro_line
- 新パラメータは pad ごとに上書き可能。pad 表は見出しの折り返しと、微小線列の折りたたみで幅を抑える
- Auto の隣に実際の方式を出す件は別 PR

## 公開インターフェース

- `pcbasm.config`
  - `DISPENSE_MODES` / `DispenseMode` に `"micro_line"` を追加
  - `PasteDispenser.nozzle_outer_diameter: float`（既定 = `nozzle_diameter`、`>= nozzle_diameter`）
  - `PasteDispenser.micro_line_z_start: float = 0.07`、`micro_line_z_end: float = 0.07`（正）、
    `micro_line_settle_seconds: float = 1.0`（0 以上）
- `pcbasm.pasting.params`
  - `PasteParams` / `PasteParamsPatch` に上記 3 項目（`PASTE_PARAM_FIELDS` 末尾、group = "微小線"）
  - `ParamField.group: str | None = None`（pad 表で畳む列のまとまり）
- `pcbasm.pasting.fill_path`
  - `AppliedDispenseMode` に `"micro_line"`
  - `FillPlan.build(..., nozzle_outer_diameter: float | None = None)`（None は内径と同じ）
  - micro_line: `_generate_linear_path(polygon, boundary_margin + outer/2)`、不成立なら dot
- `pcbasm.pasting.fill_sequence.MicroLineSequence(path, total_amount_ul, settings, settle_seconds)`
  - `to_gcode(stage, dispenser)`: 始点上空 → prime + V を同期押出 → 静定待ち → 始点へ下降 →
    path を `max_fill_speed` で走行（Z は path の点が持つ）→ 終点上空へ上昇 → 同期リトラクト
  - `effective_rate`（= max_dispense_rate）、`actual_fill_speed()`
- `PasteApplicator.apply`: micro_line の pad は始点 z_start・終点 z_end の path で `MicroLineSequence` を送る
- WebAPI: `ResolvedSettings` に 3 項目、`ParamFieldInfo.group`、設定ページの FieldSpec / layout / dispense_mode 選択肢

## テスト観点

- config: 外径の既定値・内径未満の拒否・新項目の検証
- fill_path: auto 判定（短辺 <= 内径で micro_line、超えると line）、明示 micro_line の inset が外径基準、
  不成立で dot、line_direction の適用
- MicroLineSequence: 命令順（押出 sync → 待機 → 下降 → 走行 → 上昇 → リトラクト）、量、Z
- applicator: micro_line で始点/終点 Z が z_start/z_end、実績の applied_mode
- params / persist / API: 新項目の往復、fields の group
- E2E: pad 表の折りたたみ

## 計画外の判断・記録
- 外径の既定値は attrs Factory（takes_self）をやめ、`nozzle_outer_diameter: float | None = None` と
  `effective_nozzle_outer_diameter` にした。Factory だと `attrs.evolve(config, nozzle_diameter=...)` で
  外径が古い値のまま残り、内径未満として拒否されるため。`retract_rate` / `effective_retract_rate` と同じ形。
  設定 API の resolved は None（未設定表示）になる
- 既存テストの期待値を仕様変更に合わせて直した:
  `test_outward_starts_near_component_for_every_line_resolution`（幅 0.8 / 内径 1.0 の auto は micro_line）、
  `test_auto_degenerate_sliver_resolves_without_error`（解決先の集合に micro_line を追加）
- `MicroLineSequence` は上空到達を M400 で待ってから同期 pushpull（SYNC 既定）で押し出し、
  上昇後も M400 を挟んでから同期リトラクトする
- pad 表: group ごとに開閉ボタン列を 1 つ置き、開いた group の項目だけを列にする（既定は畳む）。
  項目見出しは `white-space: normal` で折り返す
- 段階 3 ゲート: make format / make type / make test-no-hardware（3410 passed）/ E2E 145 passed

## レビュー（code-reviewer、request-changes）と裁定

- M1 / S4 採用: 設定 API が外径 >= 内径を検証せず、読めない machine.toml を保存できた。
  `config_store._validate_nozzle_diameters` で TOML の既存値とマージして検証し、テスト 2 ケースを追加
- S1 採用: `src/pcbasm/pasting/README.md` を更新（段階 5）
- S2 見送り: `config/machine.toml` は git 管理外のローカル設定。テンプレート（`data/config-templates/*`）の
  コメントだけ更新し、外径はコメントアウトした記載例にした（機体ごとにノズルが違い得るため）
- S3 申し送り: PR #34 の `tact.py` は方式によらず `FillSequence` で見積もる。両方が main に入ったら、
  後から merge する側が micro_line の静定待ちと上空押出を見積もりに足す
- N1 採用: 回転した pad で外接矩形の短辺が 0.3 をわずかに超える（0〜179° の整数角のうち 74 個）。
  許容誤差 1e-9 を入れ、許容誤差なしで落ちる回転テストを追加
- N2 見送り: `FillPlan.build` は config に依らない幾何 API なので `None` の解決を自前で持つ
- N3 見送り: プレビューの線幅は全方式共通の内径表示のまま（仕様外）
- N4 採用: pad 表のラベルを「微小線の始点高さ」などにした（override の tooltip で区別できるように）
- N5 実機確認へ: 下降は `_TRAVEL_SPEED`。飛び散りが出たら最初に疑う
- N6 採用: 定数表を写すだけのテストを削除し、defaults のテストを既定値の一致へ強めた
