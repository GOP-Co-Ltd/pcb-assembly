# docs-keeper: paste_solder の pad 有効/無効 + 階層 override 塗布設定

入力: 計画書 `~/.claude/plans/claude-webui-1-pad-extract-eager-pine.md`、
前段メモ `memory/agents/plan-implementer/{paste-hierarchy-logic, webui-pad-config-api,
webui-pad-frontend, paste-solder-integration}.md`。
ブランチ feature/20260615/paste-solder-integration（Phase 1〜5 を同系列ブランチで実装）。

## 修正したドキュメント

`docs/webui/specification.md` のみ（README / CLAUDE.md は触らず。理由は下記）。

- §9 REST/WS API 表: pad-config の 4 行追加
  （GET pad-config / PATCH node / PATCH pads / POST reset）
- §10 UI構成 pasting タブ末尾: 「Paste Solder の pad 編集（基板ビューア + 階層 override）」
  サブセクション追加。基板ビューア（SVG・クリック/矩形ドラッグ・一括）、5 階層
  override 表（L0–L4・最具体勝ち enabled・継承/override/無効の表示規則・PATCH 即保存・
  ジョブ中ロック）、override 対象 7 項目 vs マシン固定項目、責務境界を記述
- §12 ロードマップ進捗: 「Phase 5 完了後の追加機能（2026-06-15）」1 段落追記
- §15 新設「はんだ塗布の pad 設定（Phase 5 完了後の追加機能）」: 永続化
  （BoardSettingsStore・board_id・load_or_init 遅延作成・真実の源）、API 契約
  （node_id 規約・GET 一発返却・PATCH affected_pads 部分更新・検証エラー・pcbnew 遅延 import）、
  塗布実行統合（apply の per-pad override 引数・_run_paste_solder の有効 pad 抽出 +
  現行等価フォールバック）

mdformat（pre-commit hook）でテーブル整列の自動整形のみ発生。内容変更なし。

## docstring

**触っていない**。grouping.py / settings.py / board_settings.py / routers/pasting.py /
applicator.apply / jobs/pasting.py（_run_paste_solder, _resolve_paste_model,
_is_pad_enabled）の docstring は各実装者が記述済みで、シグネチャ・挙動・契約
（node_id 規約・継承=None・最具体勝ち・後方互換フォールバック）と整合しており、
明らかな欠落・不整合なし。挙動不変の制約下で追加すべき修正が無かったため無改変。
→ `.py` 無変更につき make format / make type への影響なし。

## 残した古い記述・理由

- README.md / CLAUDE.md: 本機能は paste_solder（既存 feature）への内部拡張で、
  起動手順・コマンド・プロジェクト構造（モジュール一覧の粒度）に変化なし。
  CLAUDE.md の pasting/ 説明・webui 行は既存記述で正しいまま。常時ロードされる
  CLAUDE.md に手続き的詳細を足さない方針（docs-keeper 役割）に従い追記せず。
- §1/§2/§5（スコープ・アーキ図・モジュールツリー）: 新規 file
  （grouping.py / settings.py / board_settings.py / routers/pasting.py /
  pasting/paste_solder.html / partials/pad_editor.html / pad_editor.js）は
  既存ツリー記述が「…」省略を含む粒度のため、列挙の網羅性を上げる改変はせず
  最小保守に留めた。詳細は §15 / §10 に集約。

## 後続に引き継ぐ事項

- 実ブラウザでの SVG クリック/矩形ドラッグ/表セル編集・実機塗布（@mark_hardware）は
  ユーザー検証残（前段メモ webui-pad-frontend / paste-solder-integration 参照）。
- ブランチスタックの最終マージはユーザー判断（large-refactor-workflow 規約）。
