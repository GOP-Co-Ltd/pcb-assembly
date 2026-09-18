# machine.toml の machine_name 廃止

## 要件

- `machine.toml` の設定項目 `machine_name` を削除する
- 機体名は OS のホスト名（= `machine_id`）。同名ホストは IP で見分ける
- 最初から実装予定になく、運用でも使っていない

## 方針

**「自由入力の表示名」という概念を無くす。名前が要る所はホスト名をそのまま使う。**

| 経路 | 対応 |
| --- | --- |
| `machine.toml` の `machine_name` | 削除（`Machine.machine_name` / `FieldSpec` / `SECTION_LABELS`） |
| `AppState.machine_name()` | 削除 |
| `MachineInfo.machine_name` | 削除（`machine_id` がホスト名そのもの） |
| mDNS TXT の `name` | 削除（TXT の `id` と同値になる）。`ServiceAdvertiser.update` と改名再広告も削除 |
| frontend の TXT `name` 読み取り | 削除。`MachineEndpoint.name` は `machines.toml`（別ファイル）由来のみ残す |
| dataset metadata の `machine.name` | **キーは残しホスト名を書く**（schema v3 は `forbid_extra_keys`。既存 dataset を読めなくしない） |

## ユーザー確認

- dataset metadata の `machine.name` をどうするか → 「OS のホスト名を machine_name として扱ってください」
    - → schema v4 へ上げず、`machine_id` をそのまま `name` に書く

## 却下した案

- dataset schema を v4 に上げて `machine.name` を削除 → 既存 dataset に移行関数が必要になり、収集済みデータを読めなくする
- `MachineInfo.machine_name` を `machine_id` と同値で残す → 同じ値を 2 つ返すだけで読み手が無い
- frontend `machines.toml` の `name` / `MachineEndpoint.name` / `MachineSummary.name` の削除 → 変更前から表示に未使用（`label` は `machine_id: host`）で、今回の変更が生んだ不要コードではない（AGENTS.md 開発原則 3）

## 自己レビューの指摘と対応

- `build_service_info` の `instance` 引数が `ServiceAdvertiser.update` 廃止で到達不能になる
    → 引数ごと削除し、`test_instance_override_keeps_the_registered_name` も削除
- `_merge` / `_fill_gaps` の docstring が「`name` も mDNS で埋める」と書いたまま
    → mDNS 側が `name` を持たなくなったので記述を `machine_type` だけに直す
- worktree の `.venv` が `include-system-site-packages = false` で `make type` が
    pcbnew / picamera2 を解決できず 15 error（変更前後で同数）。`.venv/pyvenv.cfg` を
    primary checkout と同じ `true` に直して 0 error。コードの問題ではない

## 残した箇所（今回の変更が生んだ不要コードではない）

- frontend `config/machines.toml` の `name` と `MachineEndpoint.name` /
    `MachineSummary.name`：静的登録という独立した供給源があり、変更前から `label`
    （`machine_id: host`）に使われていない
- `memory/agents/**` の過去タスク記録に残る `machine_name` 記述：当時の事実の記録なので
    書き換えない

## テスト観点

- `Machine` に `machine_name` が無い
- `PUT /api/settings/machine` に `machine_name` を渡すと `UnknownFieldError`（未知キー）
- `GET /api/settings/machine` の fields に `machine_name` が出ない
- `GET /api/machine-info` の応答に `machine_name` キーが無い
- mDNS TXT に `name` キーが載らない
- dataset の `machine.name` が `machine_id` と一致する
