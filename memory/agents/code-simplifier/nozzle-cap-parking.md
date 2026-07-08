# ノズルキャップ位置（nozzle-cap-parking）簡素化ノート

計画書: `memory/agents/implementation-planner/nozzle-cap-parking.md`
対象: `git status` の未コミット変更全体（src/ + tests/ + configs/）。公開 IF・API 契約・
テストが固定した substring 契約は一切変更していない。

## 適用した簡素化（2 件）

1. **`src/pcbasm/parking.py` — send_present_or_relax フォールバック 3 箇所を 1 箇所に集約**
   - 旧: 3 つの guard clause がそれぞれ `klipper.send_present_or_relax(warn=warn, timeout=timeout)` + `return` を持つ
   - 新: try/except/else で「駐機先 cap の決定（warn はここで発火）」と「送信」を分離し、
     `cap is None` の単一分岐でフォールバック。`send_present_or_relax` 自体（hal/klipper.py）と
     同じ try/except/else + 末尾単一フォールバックの家風に揃えた
   - 単純化の理由: フォールバック呼び出し（warn/timeout 転送）の記述が 1 箇所になり、
     「cap が得られなければフォールバック」という契約が構造に現れる。新規ヘルパー関数は導入していない
   - 挙動は不変: except は `machine.machine_type` のみを包む（`else:` 内の
     `machine.nozzle_cap` の例外は従来どおり伝播）。warn 文言（"machine_type"/"nozzle_cap" 含有）不変

2. **`src/pcbasm/gcode.py` — `CAP_PARK_VELOCITY` の整形**
   - 行末コメントによる `( 20.0 # ... )` の括弧折り返しをやめ、コメントを定数の直前行へ移動

## 点検して「変更不要」と判断した点

- `config.py machine_type`: `_get_config` 再利用は可能だが、計画書が「トップレベルスカラーは
  `self._data` 直読み」と明記しており現状維持
- `nozzle_cap.py` router / `machine_commands.py` / `machine_control.py` / `pages.py` /
  `state.py machine_type()`: いずれも既存の同種コード（STATUS_TIMEOUT 定数、focus_z の
  broad except→None、`_xxx_context` provider、case 節）の流儀どおりで、簡素化の余地なし
- `machine_control.js` の null ガード: ボタンが `machine_type == "paste"` 時のみ描画されるため必要
- tests/: `[nozzle_cap]` 追記ブロックが 3 ファイルに 1 回ずつ現れるが、各テストの setup を
  ローカルに保つ意図（spec-test-author ノート）を尊重し共有ヘルパー化しない

## 検証

- `make format` / `make type`（pyright 0 errors）/ `make test-no-hardware`（1500 passed, 76 deselected）
- 実機テスト（@mark_hardware / make test / e2e）は未実行（実機接続のため禁止・ユーザー残）
