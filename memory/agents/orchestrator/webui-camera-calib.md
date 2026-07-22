# orchestrator ノート: webui-camera-calib

ブランチ: `feature/20260722/webui-camera-calib`（worktree、main から分岐）
計画書: `memory/agents/implementation-planner/webui-camera-calib.md`

## 委譲判断

- 事前調査は Explore agent に委譲（カメラキャリブページの構成・永続化機構・overlay 経路を特定）
- 計画は implementation-planner に委譲。公開 IF がシグネチャレベルで確定したため、
  パターン A（spec-test-author ∥ plan-implementer）で並列起動
- テスト編集は spec-test-author に一本化（plan-implementer は tests/ を触らない）

## 計画の要確認事項への裁定

1. **square_size の入力途中即保存 → 採用**。
   ユーザー要件「入力値を保存しておきます」の素直な解釈。loading ページの
   debounce POST param-defaults と同機構で追加コストが小さい。
2. **camera.crop.\* の 1 以上バリデーション → 採用**。
   change 即自動保存 UI では 0/負値が machine.toml に書かれる事故が現実的に起きる
   （起こり得ないシナリオへの過剰防御ではない）。config_store の既存 per-key 検証
   （max_failures >= 0）の前例に準拠し、検証のサーバ集約は thin-wrapper 原則と整合。

## 事前作業（orchestrator 自身）

- main の未コミット変更 `configs/kurousagi/machine.toml`（crop 600→300）を
  git diff | git apply で worktree に取り込み済み（ユーザー要件 5）
- worktree venv 再作成済み（`uv venv --clear --system-site-packages && uv sync --all-extras`）
- ブランチ名を規約形式に改名（worktree-feature+... → feature/20260722/webui-camera-calib）

## レビュー裁定（code-reviewer verdict: request-changes）

- **must-fix 採用**: `PreviewService._crop_size()` のフレーム毎 toml 読みと
  `ConfigStore.write_machine_settings` の非アトミック `write_text` の競合で
  MJPEG ストリームが落ちる（reviewer が実測 17% 衝突で再現）。
  → plan-implementer に差し戻し（アトミック書き込み: 同一ディレクトリ temp + os.replace）
- **should-fix 採用**: 混在 PUT（camera.fps + camera.crop.*）の rebuild 挙動が
  テストで未ピン。テスト追加のため code-simplifier ではなく spec-test-author に委譲
  （tests/ 専用の担当区分を維持）
- **却下ではなく別タスク送り**: settings.js の data-machine-settings 汎用機構と
  camera_calibration.js の重複統合。理由: 計画・裁定済み IF をテストがピン済みで、
  統合はテスト張り替えを伴い今回の外科的範囲を超える（reviewer 判断を支持）。
  ユーザーへ別タスク候補として報告する

## 追加要件（ユーザー指示・MR !137 提出後）

「汎用フォーム機構の重複」の解消として、**crop 値の編集 UI を settings ページのみに統一**
することをユーザーが決定。カメラキャリブページの crop 即保存フォームは撤去する。

- 撤去: camera_calibration.html の crop fieldset / camera_calibration.js の
  bindCropAutoSave / pages.py の _CAMERA_CROP_KEYS・_camera_calibration_context・
  _FEATURE_CONTEXT エントリ
- 維持: フレーム毎 crop 読み（settings ページからの保存でも即時反映）、
  camera.crop.* の rebuild 除外、atomic 書込、≥1 検証、square_size 1.5 + 永続化、
  専用テンプレート（square_size 保存 JS の読み込みに必要）
- settings ページは data-machine-settings 汎用フォームで camera.crop.* を既に表示
  （pages.py:266 machine_settings_fields → settings.html:72）ことを orchestrator が確認済み
- 小規模のため計画は orchestrator 自身が策定（planner 不使用）

## 申し送り

- main 直下に不審な untracked ファイル `"\0014\253\006@W@8"` あり（本タスクと無関係。
  触らずユーザーに報告する）
- 実機確認（実カメラでの 1.5mm ボードキャリブ・ブラウザ体感）はユーザーに委ねる
