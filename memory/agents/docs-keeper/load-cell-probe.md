# docs-keeper: load-cell-probe

サーボ+電気接点プローブ → Klipper [load_cell_probe] 移行に伴うドキュメント最小保守。

## 修正したファイル

- `configs/README.md` — install-printer-cfg.sh の新挙動を 1 段落追記
  （klipper.env の config パスを repo 実パスへ・SAVE_CONFIG 較正値が git diff に
  現れる・symlink は閲覧用・要 Klipper 再起動）
- `.claude/skills/testing-strategy/SKILL.md` /
  `.agents/skills/testing-strategy/SKILL.md`（両コピー）—
  削除済みクラスの言及を除去：HAL ABC 例示 `Servo`, `Probe` を削除、
  実機テスト対象の「実サーボ」→「実ロードセル」

## 修正不要と判断した箇所

- ルート README・全モジュール README（hal/pasting/posctrl 等）—
  プローブ/サーボへの言及なし（grep 済み）
- 変更モジュールの docstring — plan-implementer が整備済み
  （config.Probe「ロードセルプローブの設定」、ProbeExecutor の
  [load_cell_probe] 検証 docstring 等）で不整合なし
- `src`/`tests` 内の probe_gnd 言及 — 削除確認の回帰テストで意図的な残存
- `memory/agents/*` の過去メモ — 履歴記録のため触らない
