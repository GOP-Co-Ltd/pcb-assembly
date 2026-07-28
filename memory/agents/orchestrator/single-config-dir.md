# single-config-dir — orchestrator の判断ログ

`configs/<machine>/` 廃止 → 単一 `config/`（git 管理外）への再編。
計画書: `/home/gop/.claude/plans/claude-configs-config-git-configs-1-glowing-glade.md`

## ユーザー判断で確定した設計

| 論点 | 決定 |
| --- | --- |
| マシン名の概念 | 完全廃止（`selected_machine` / `select_machine` / `list_machines` / `default_machine` / `StateResponse.machine` / ヘッダの select） |
| テンプレート | `kurousagi.paste` のみ。`kurousagi002` はテンプレート化せず破棄（実機設定は `config/` へ移行） |
| テスト fixture | `data/testing/config/` を追加。`data/testing/machine.toml` とは併存 |
| パス解決 | `PCBASM_CONFIG_DIR` env に統一（pcbasm コアと webui の二重解決を解消） |
| `PasteSession.setup()` | 呼び出し元 0 件の dead code だがシグネチャ修正のみ（削除しない） |
| printer.cfg の drift | 仕様として許容。テンプレートへの手動書き戻し手順を README に記載 |

## 計画からの逸脱（実行順の最適化）

計画の Phase 2 を **2a / 2b に分割**した。Klipper が live 参照しているのは
`configs/kurousagi002/` だけなので、その削除のみを最後に回せば残りの Phase を
ユーザーの実機作業と並行して進められる。`configs/kurousagi002/` は Phase 2b として保留中。

## レビュー指摘の裁定

verdict は approve（must-fix なし）、should-fix 4 件 + nit 10 件。

### 昇格: S1 を must-fix に格上げして修正

`setup-machine-config.sh` が `machine.toml` を「既存なら常にスキップ」で守る一方、
`printer.cfg` は無条件にテンプレートで上書きしていた。`SAVE_CONFIG` が
`load_cell_probe` の較正値や `position_endstop` を追記するため、`printer.cfg` も
実測値が蓄積する正である。設計の非対称性であり、しかも**計画書自身が
「冪等性確認のため 2 回実行」をユーザーに指示していた**ため、指示に従うと較正値が消える。

修正: `printer.cfg` も実ファイルが存在すれば保持する。
- symlink → 削除して実ファイル化（旧レイアウトからの変換）
- 実ファイル → **スキップ**（較正値を守る）
- 不在 → テンプレートを配置

symlink と klipper.env の張り直しは毎回行うので、修復目的の再実行は引き続き機能する。
`scripts/migrate_config_layout.sh` にも同じ保護を適用。偽 HOME に `SAVE_CONFIG` ブロック入りの
`printer.cfg` を置いて実測し、保持されることと 2 回目が完全スキップになることを確認した。

### 採用: S2 / S3 / S4（code-simplifier に委譲）

- S2 `app.css` の `.settings-header p` orphan — 自分の変更で生じた orphan（CLAUDE.md 原則 3）
- S3 2 系統の `machine.toml` の用途差を README の役割表と `tests/webui/conftest.py` docstring に明記
  — 計画書リスク 6 が要求していたのに未達だった
- S4 `legacy_root` が到達不能になった旨を docstring に明記（削除はしない）

### 却下

| 指摘 | 理由 |
| --- | --- |
| `create_app()` の fail-fast を壊れた TOML まで検査 | 投機的実装。存在チェックで十分（CLAUDE.md 原則 2） |
| `_SCHEMA_VERSION` を上げる | 既存データがディスク上に無く、実害方向は安全側 |
| `test_corrupted_state_file_falls_back_to_default` が `test_initial_pcb_is_none` と区別しにくい | 「壊れた state file でも起動する」は別の保証 |
| `data/testing/config/printer.cfg` が dead | 既存 dead。触らないのが正しい（CLAUDE.md 原則 3） |
| `pages.py` の `_paste_solder_context` の未使用 `state` | `_FEATURE_CONTEXT` dict の共通シグネチャ由来。残置妥当 |

## 計画書の欠落（実施側が補完）

- `tests/webui/test_board_settings.py` を機械的置換リストから漏らしていた。
  `BoardSettingsStore` から machine 引数が消える当然の波及。spec-test-author が自ら発見して対応
- `src/webui/jobs/context.py` の `ApplyFile` / `ApplyPayload` docstring の `configs/<machine>/` 表記。
  plan-implementer が完了条件の grep 0 件を満たすために補完

## 別タスク送りにしたもの

**`BoardSettingsStore(legacy_root=...)` は今回の変更で明確に dead になった。**
`_doc_path` の legacy 参照先が `data/board_settings/<board_id>.json` になり、旧レイアウト
`data/board_settings/<machine>/<board_id>.json` と永久に一致しない。両ディレクトリともディスク上に
存在せず実害なし。`app.py` の `legacy_root=` 配線と
`test_legacy_root_is_read_only_fallback` ごと削除するのが妥当だが、元から dead の可能性が高いため
CLAUDE.md 原則 3 に従い指摘に留めた。

## 検証

`make format` / `make type`（0 errors）/ `make test-no-hardware`（1607 passed, 87 deselected）。
残骸 grep 0 件、`</content>` 混入なし、`make migrate-codex-check` OK、
`.gitignore` アンカー回帰（`config/` 無視・`data/testing/config/` 追跡）OK。
実機テストは未実行（`memory/MEMORY.md` の No hardware test execution）。
