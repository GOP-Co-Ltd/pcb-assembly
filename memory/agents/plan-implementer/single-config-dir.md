# configs/ 廃止 → 単一 config/（src/ 側 Phase 3a）

## 計画外の判断ログ

- **`Settings.from_env()` の `config_dir` 行を完全削除**（計画通り）。`base = cls()` 時点で
  `attrs.field(factory=get_config_dir)` が env を解決しているが、`return cls(...)` でも
  factory が再評価されるため結果は同値。`config_dir=base.config_dir` を明示するのは冗長なので置かなかった。
- **ステップ実行順を 3 → 4 ではなく「2 の残り（app.py / state.py）を 4 と併せて先に片付け → 3」で進めた**。
  Step 2 で state.py に「型が通る暫定修正」を入れると Step 4 で捨てる作業になるため。
  最終状態は計画通りで、各段の diff 内容は変わっていない。
- **`ApplyFile` / `ApplyPayload` の docstring を `configs/<machine>/` → `config/` に修正**（`jobs/context.py:65,78`）。
  計画のステップ一覧に無いが、完了条件 `grep -rn "configs/" src/` = 0 件に必要。
- **`AppState` / `ConfigStore` / `common.create_klipper` の docstring から「選択マシン」表現を削除**。
  マシン選択の概念が消えたため文言が嘘になる。振る舞いの変更はない。
- **`_persist` の後方互換コードは書いていない**（計画通り）。既存 `webui_state.json` の
  `machine` キーは次の書き込みで消える。
- `settings.html` の `<div>` は `<h1>マシン設定</h1>` 1 要素だけを包む形になった。
  `.settings-header` のレイアウトは div 前提なので残置（構造変更はスコープ外）。

## 触っていないもの（指摘のみ）

- **`BoardSettingsStore(legacy_root=...)`** — machine セグメント除去後、`_doc_path` の legacy
  fallback は `data/board_settings/<board_id>.json` を見る。旧レイアウトは
  `data/board_settings/<machine>/<board_id>.json` だったので**もう一致しない**。両ディレクトリとも
  ディスク上に存在せず実害なし。計画の指示どおり削除していないが、`app.py:95-97` で
  `legacy_root=` を渡す配線ごと dead であり、次のタスクで削除候補。
- **`PasteSession.setup()`** — 呼び出し元 0 件。シグネチャのみ修正、削除せず（ユーザー判断）。
- **`pages.py` の feature context の未使用引数** — `_paste_solder_context(state, ...)` の `state` と
  `_nozzle_cap_context(..., store)` / `_copper_detection_context(..., store)` の `store` が未使用。
  `_FEATURE_CONTEXT` dict の共通シグネチャ `Callable[[AppState, ConfigStore], ...]` に縛られるため残置。
  `_paste_solder_context` の `state` 未使用化は今回の変更が原因（`state.selected_machine` を落としたため）。

## 他 implementer への IF 変更通知

計画書で固定された公開 IF から逸脱していない。テスト側で追加確認すべき点のみ:

- `webui.routers.app_state` に残るのは `GET /api/state` のみ。`MachineSelect` モデルは消滅。
- `create_app()` は `config_dir/machine.toml` が無いと `RuntimeError`（起動時 fail-fast）。
  `Settings(config_dir=...)` を渡す fixture は必ず `machine.toml` を含むディレクトリを指す必要がある。
- `PadConfigResponse` / `StateResponse` / `MachineSettingsResponse` から `machine` が消えた。
- `data/webui/board_settings/<board_id>.json`（machine セグメントなし）。

## 検証結果

- `make format`: pass
- `make type`: **src/ にエラー 0 件**（残る 62 件はすべて `tests/` 配下 = spec-test-author の担当範囲）
- `make test-no-hardware`: 未実行（指示により tests/ 移行完了まで赤が正常）
- スモーク:
  - `get_config_dir()` = `PROJECT_ROOT/config`、`PCBASM_CONFIG_DIR=/tmp/x` で `/tmp/x`（呼び出し時評価）
  - `Settings().config_dir` / `Settings.from_env().config_dir` ともに解決 OK
  - `create_app()` の routes に `/api/machine` 不在・`/api/state` 存在
  - `create_app(Settings(config_dir=<不存在>))` が RuntimeError で fail-fast
  - `/posctrl` `/pasting` `/settings` `/pasting/probe_guide` が 200、レスポンス JSON に
    `machine` キー無し、HTML に `machine-select` 無し
- 残骸 grep: `configs_root|select_machine|selected_machine|default_machine|list_machines|
  PCBASM_WEBUI_CONFIGS_ROOT|machine_name|configs/` が `src/` で 0 件
