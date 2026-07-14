# docs-keeper: board-corner-calibration

対象ブランチ: feature/20260714/board-corner-calibration

## 変更内容

- `configs/README.md` — machine.toml 例に `[reference_point]`（新形式: corner / x / y / target_diameter / offset）と `[board_align]`（全キー既定値あり・節省略可、WebUI 編集には行が必要）を追記。直後に1段落で新方式を説明（アンカー1コーナーのみマーカー必須、board 変換は基板4隅の外形輪郭照合・6DOF 最小二乗フィット、bbox 4隅 ±edge_length の外形ジオメトリ要件と `edge_length + search_window ≤ 視野短辺/2` の制約）。値・コメントは configs/test-fixture/machine.toml に整合
- `src/pcbasm/posctrl/README.md` — 先頭 bullet に計測方式（アンカーマーカー1点サーボ → 4隅輪郭照合の最小二乗フィット）を付記

## 確認事項

- 旧仕様（3点マーカー・CornerOffsets・offsets テーブル・get_reference_position）を記載した *.md は他に無し（grep 済み。.agents/skills のヒットは pytest/conflict マーカーで無関係）
- configs/README.md には元々 reference_point の記載なし → 追記で対応
- 新規ドキュメントは作成していない（本メモを除く）
