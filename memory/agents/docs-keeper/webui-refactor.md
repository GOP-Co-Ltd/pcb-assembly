# webui 大規模リファクタ（Group A〜E）後のドキュメント整合

ブランチ: refactor/20260707/webui-tests

## 実施内容

- README.md: 削除済み `docs/webui/specification.md`（main の abb1610 で削除）への dead link を除去。
  他の記述（make コマンド・環境変数）は現状と整合。
- stale 記述の grep 確認: `snapshot` / `interactive_loading` / `pad_editor.js` /
  `/api/machines` / `pad-config/reset` / `pcbasm.pasting.loading` を src/・README で走査。
  docstring/コメントに stale な言及なし。
  - `src/webui/jobs/pasting.py` の `interactive_loading` はジョブパラメータ名として現役（削除された
    pcbasm モジュールとは別物）。
- 新規モジュール（dependencies.py, routers/common.py, pasting_view.py, pasting_loading.py,
  jobs/board_ops.py, jobs/context.py, pad_editor/index.js, table.js）の docstring/ヘッダコメントは
  実装と整合済み。追記不要。

## 未適用（ユーザー確認待ち）

- CLAUDE.md「プロジェクト概要」の `pasting/` 説明に削除済み `loading` が残存。
  提案: `（applicator, calibration, fill_path, loading, probe, height 等）` →
  `（applicator, calibration, fill_path, probe, height 等）`。
  CLAUDE.md はユーザー確認が必要なため未変更。
