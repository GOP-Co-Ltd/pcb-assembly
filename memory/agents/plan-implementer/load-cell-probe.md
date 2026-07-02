# plan-implementer: load-cell-probe 実装メモ

## 状態

src/ + configs/ + .gitignore の計画分は実装完了。tests/ は未タッチ（spec-test-author 管轄）。

- `uv run pyright src` — 0 errors
- `make format` — pass（初回で ruff-format/docformatter が自動整形、再実行でグリーン）
- `make type`（全体）— tests/pcbasm/pasting/test_probe.py の 2 エラーのみ residual（下記）

## spec-test-author への差し戻し・連絡事項

- `tests/pcbasm/pasting/test_probe.py:73, :89` — `klipper.send_gcode.call_args_list` に
  pyright エラー（`MethodType` に `call_args_list` は無い）。fake/mock の型付けの問題で
  実装側の問題ではない。Mock 型注釈（`cast` や `Mock` 属性化）での修正を依頼。

## 計画外の判断・気づき（現タスクでは未対応）

- `config_store.py` の `float_pair` 型は probe.shift 削除により FieldSpec での利用が
  ゼロになった（`_coerce_float_pair` / settings.html の float_pair 分岐 /
  settings_api.py の Literal が orphan 候補）。計画外のため残置。
  code-simplifier または別タスクでの除去を提案。
- `pasting/height.py` の `_BoardPointProber._probe_position` は shift 削除後、
  `board_to_machine.apply(board_pt)` を返すだけの薄いヘルパになった。
  `route_points` の key と `probe_at` で共用しているため残置（simplifier 判断に委ねる）。
- `install-printer-cfg.sh` に未コミット変更が既に存在する（本タスクでは触っていない。
  主ループのコミット分割時に注意）。

## IF 変更通知

なし（計画書のシグネチャどおり）。
`ProbeExecutor.__init__(klipper, stage, *, lift_height=1.0, settle_time=0.0)` /
`probe() -> float`。RuntimeError メッセージ:
「printer.cfgに[load_cell_probe]セクションを追加してください」
