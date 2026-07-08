# ノズルキャップ位置（nozzle-cap-parking）ドキュメント整備ノート

計画書: `memory/agents/implementation-planner/nozzle-cap-parking.md`

## 修正した内容（1 件）

- `configs/README.md`: machine.toml のサンプルに必須キー
  `machine_type = "paste" # マシン種別: paste / pnp（必須）` を追加。
  欠落するとアクセス時 KeyError になる必須キーのため、テンプレートとして
  コピーされる例に載せる必要があった。

## 点検して「修正不要」と判断した点

- `[nozzle_cap]` の README 追記: 見送り。サンプルは全テーブルを列挙しない方針
  （paste_dispenser / probe / reference_point も未記載）で、nozzle_cap は任意かつ
  WebUI から記録するもの。手書き前提の必須キーだけ例示すれば足りる
- ルート README / モジュール README（hal, posctrl, pasting 等）: machine.toml
  スキーマ・PRESENT・終了動作への言及なし（grep 済み）。WebUI タブの列挙もなし
- docstring: 新規 `parking.py`（module + park_or_present）・`routers/nozzle_cap.py`
  は実装と整合する日本語 docstring 済み。変更 IF（`Machine.machine_type` /
  `Machine.nozzle_cap` / `gcode.move_to_cap` / `machine_session` /
  `PasteSession` / `AppState.machine_type` / `_park_machine`）も plan-implementer が
  更新済みで不足なし
- `</content>` 混入チェック（feedback-agent-content-artifact）: src/tests/configs/
  memory/agents に混入なし

## 検証

- `make format`（mdformat 含む）: pass
- テスト実行はなし（ドキュメントのみの変更。実機接続のため make test 禁止）
