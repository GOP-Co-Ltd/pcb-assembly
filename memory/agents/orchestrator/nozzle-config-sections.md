# ノズル位置設定を paste_dispenser 配下へ移す

ブランチ: `refactor/2026-09-14/nozzle-config-sections`
ベース: `feature/2026-09-14/nozzle-clean`（MR !234。stacked MR として出す）

## 要件（ユーザー指示）

- `[nozzle_cap]` → `[paste_dispenser.nozzle_cap]`
- `[nozzle_clean]` → `[paste_dispenser.nozzle_clean]`
- どちらもペーストマシン固有の位置なので paste_dispenser 配下が筋
- 旧トップレベルキーは**自動移行**する（ユーザーの指示で「検出して警告」から変更）

## 自動移行の形

2 段構え。設定ファイルを手で直さなくてよく、放置しても新旧が二重に残らない。

1. **読み込み時**: `[paste_dispenser.*]` に無ければ旧トップレベルを読む（新が優先）。
    設定を書き換えなくても動き続ける
2. **書き込み時**: `ConfigStore.write_machine_settings` が旧セクションを新パスへ移して
    削除する。記録ボタンや設定保存の初回でファイル自体が移行される

## 設計

### 移設先は PasteDispenser の属性

`pad_align` / `flow_calibration` が既に `[paste_dispenser.pad_align]` として
`PasteDispenser` の属性になっている前例に合わせる。

### Machine の公開 IF は変えない

`Machine.nozzle_cap` / `Machine.nozzle_clean` は `paste_dispenser` への委譲として残す。
TOML のパスが変わるだけで呼び出し元（parking / state / nozzle_clean）は無変更。
pnp マシンには `[paste_dispenser]` が無いので、その吸収も 1 箇所で書ける。

### 不完全サブテーブルで paste_dispenser 全体を落とさない（要）

実験で確認: 座標の欠けた `[paste_dispenser.nozzle_clean]` があると cattrs は
**PasteDispenser 全体**の structure に失敗する。トップレベルだった頃は
`Machine.nozzle_clean` だけが失敗し paste_dispenser は無事だったので、素直に移すと

- `/settings` の塗布パラメータが全て「未設定」に見える
- `_MachineSettings.number()` を使うページ（copper_detection / loading / paste_solder）が 503
- 塗布ジョブが `result.machine.paste_dispenser` で落ちる

という退行になる。設定ページから押し込み量だけ保存すれば起きる、普通の状態。

対策: cattrs の structure hook で不完全なサブテーブルを None（＝未記録）へ落とす。
座標に既定値を持たせる案は原点へ移動する事故が戻るので採らない。

## 変更範囲

| 対象 | 内容 |
| --- | --- |
| `config.py` | `NozzleCap` / `NozzleClean` を `PasteDispenser` の属性へ。委譲プロパティ。structure hook。旧キー検出 |
| `config_store.py` | `MACHINE_FIELDS` のキーを `paste_dispenser.` 付きへ |
| `ui/layout.py` | `SECTION_LABELS` と `NOZZLE_CLEAN_SETTING_KEYS` / `POSITIVE_ONLY_MACHINE_KEYS` のキー |
| `routers/nozzle_cap.py` | `write_machine_settings` の prefix |
| TOML 実体 | `config/machine.toml`（gitignore・手で直す）/ `data/config-templates/*` / `data/testing/*` |
| 旧キー警告 | `Machine` が検出 → `/api/state` → ノズル位置ページに表示 |
| テスト | キー文字列を使う箇所。新規に「不完全サブテーブルでも paste_dispenser が読める」を固定 |

## 進捗

- [x] 段階 1 計画
- [x] 段階 2 テスト
- [x] 段階 3 実装
- [x] 段階 4 自己レビュー + code-reviewer（must-fix 2 / should-fix 7 / nit 3）
- [ ] 段階 5 ドキュメント
- [ ] MR

## 実機 config/machine.toml について

ユーザーが既に `[nozzle_clean]` を実設定していた（x=67 / y=54 / z=-36 /
press_depth=0 / purge_ul=0.1 / stroke=1.0 / passes=2 / wipe_speed=1.0）。
gitignore 配下なので手で移設し、tomllib で読み比べて値が変わっていないことを確認済み。
`[paste_dispenser.nozzle_cap]` ともども paste_dispenser 群の直後へ置いた。

## 実装中の判断

- `Machine.nozzle_cap` / `nozzle_clean` は `paste_dispenser` プロパティを経由せず
    `_data` からサブテーブルだけを読む。経由すると `[paste_dispenser]` の他のキーの
    不備に巻き込まれ、`park_or_present`（try の外で読む）が例外で落ちてクリーンアップ
    そのものが動かなくなる
- 不完全なサブテーブルは structure hook で None（未記録）へ落とす。素直に structure
    させると PasteDispenser 全体が読めなくなり、塗布パラメータが軒並み失われる

## レビュー指摘への対応

- **M1 無音のデータ喪失**: `del doc[name]` が「入れられるか」の判定より前にあり、
    `[paste_dispenser]` 群が分断された toml（tomlkit が proxy を返す）では旧セクションを
    消したうえで新パスにも入らず、教示済みの座標が失われていた。入れられると確かめてから
    消す順序に直し、回帰テストを置いた
- **M2 + S2 + N1**: 読み替えを `Machine.__init__` の `_merge_legacy_nozzle_sections` へ
    一本化した。`paste_dispenser` 経由で読む消費者とノズル専用アクセサで値が食い違わなく
    なり（未移行ファイルで /settings が「未設定」・ノズル位置ページが「記録済み」と並ぶ
    問題も消える）、使われないまま残っていた `legacy_nozzle_sections` も削除できた
- **S5 コメントの孤立は仕様として受け入れ**、テストで明示した。tomlkit ではセクション
    直上の独立コメントがテーブルとは別要素で一緒に移せない。行内コメントは保たれる
