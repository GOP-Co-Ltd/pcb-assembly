# 設定ページの階層化・並び替え・2 ペイン化

## 要求（ユーザー）

- 設定ページが階層的に整理されていない
- ペーストディスペンサーの設定セクションが 2 つに分かれていて扱いづらい
- 設定数が増えたので適切な並び替えが要る
- 縦に連ねるのは限界。設定表示 UI を改修する

レイアウト方針はユーザー選択で **「2 ペイン（左セクションナビ + 右パネル）+ 絞り込み検索」**。

## 現状の問題（コードで確認）

- 並び順の正は backend `MACHINE_FIELDS`（71 項目）の定義順。frontend `grouped_fields()` は
    **連続する同一セクションを `groupby` でまとめるだけ**
- `MACHINE_FIELDS` は `paste_dispenser` 直下 18 項目 → `flow_calibration` 4 項目 →
    `paste_dispenser` 直下 3 項目（bead_width_factor / overlap / boundary_margin）の順。
    連続でないので **「ペーストディスペンサー」カードが 2 枚に割れる**（ユーザー指摘の現象）
- 階層は `"ペーストディスペンサー / パッド位置合わせ"` とラベル文字列に `/` を入れて表現
    しているだけ。DOM は 13 グループがフラットな `<details>` カードの並び
- `paste_dispenser` 直下 18 項目の並びが混在（回転数・径・方式・速度・リトラクト・高さ・
    プライム・パージ…）
- `SettingsField.resolved`（実効値）は settings ページで一切表示していない。未記載キーは
    空欄 + placeholder「未設定」で、装置が実際に使う既定値が読めない

## 設計

### 表示構造の所有者

現状の分担（`layout.py` docstring）どおり **表示のまとめ方は frontend が持つ**。
`SECTION_LABELS` + `groupby` を捨て、frontend に明示的なレイアウト宣言を置く。

```python
SettingsGroupSpec(label, keys)            # カード 1 枚 = 項目の順序付き列
SettingsSectionSpec(slug, label, groups)  # 左ナビ 1 項目 = 右ペイン 1 画面
SETTINGS_SECTIONS: tuple[SettingsSectionSpec, ...]

SettingsGroup(label, fields) / SettingsSection(slug, label, groups)  # 描画用
settings_sections(fields) -> list[SettingsSection]
```

- 並び順は `SETTINGS_SECTIONS` が正になる（backend の `MACHINE_FIELDS` 定義順に依存しない）。
    ペーストディスペンサーの分裂は構造的に起きなくなる
- 宣言漏れは **末尾の「未分類」セクション**へ落とす（設定が唯一の編集画面から消えるのを防ぐ）。
    既存の `SECTION_LABELS.get(section, section)` フォールバックと同じ役割
- 網羅は `PASTE_VOLUME_CALIBRATION_PARAM_GROUPS` と同じ形のカバレッジテストでピンする
    （`MACHINE_FIELDS` の全 key がちょうど 1 回出る）

### セクション構成（並び替え後）

1. ペーストディスペンサー（49）: 吐出の基本 / 吐出の速度 / リトラクト / プライム・パージ /
    塗布方式 / 塗布経路 / Z 高さ / ツールヘッド / パッド位置合わせ / 流量キャリブレーション /
    ノズルキャップ / ノズルクリーニング
2. プローブ（5）
3. 基準点（7）: 位置 / コーナーオフセット
4. カメラ（8）: デバイス / クロップ
5. 通知音（2）

### ページ構成

- 左: セクションナビ（`<button data-section>`）。右: 選択中セクションのみ表示、
    カードは `auto-fit` の段組みグリッド
- ナビと絞り込み入力は **`<form data-requires-control>` の外**に置く。中に入れると操作権が
    無いとき `inert` になり、閲覧のためのナビ操作すらできなくなる
- 絞り込みは項目行の `data-search`（ラベル + ドットキー）に対する部分一致。ヒットが 0 の
    カード・セクションを隠し、絞り込み中は全セクションを横断表示する
- placeholder に実効値（`field.resolved`）を出す。value には入れない（入れると未記載キーが
    保存フォームに乗って machine.toml へ書き戻る。`SettingsField` の docstring の契約）

### 触らないもの

- backend（`MACHINE_FIELDS` / `/api/settings/machine`）。項目と値の契約は変えない
- `settings.js`（即保存ロジック）。paste_solder / nozzle_cap / audio / pad_editor と共用。
    依存する DOM 契約（`name` / `data-type` / `data-allow-empty` / `.settings-pair[data-pair-key]` /
    `form[data-endpoint]`）は維持する。ナビ + 絞り込みは設定ページ専用の別 JS に置く

## 段階

- 2 テスト: `settings_sections()` の構造・カバレッジ・未分類フォールバック、ページ描画
- 3 実装: layout.py / settings.html / app.css / settings_nav.js
- 4 自己レビュー + `code-reviewer`（ユーザー指定）
- 5 ドキュメント: layout.py docstring、必要なら README

## 併走ブランチとの衝突（申し送り）

`feature/2026-09-18/settle-config` が `SECTION_LABELS` に `settle` / `detection` を追加する。
本 PR はその dict を削除するので **merge 時にコンフリクトする**。解決は「新キーを
`SETTINGS_SECTIONS` のセクションとして足す」。放置しても未分類セクションに出るだけだが、
カバレッジテストが落ちて気付ける。

## 実装の実際

- `SETTINGS_SECTIONS`（5 セクション / 21 カード / 71 項目）を frontend に置き、
    `SECTION_LABELS` と `grouped_fields()` を削除。`section_of()` は未分類の束ね直しで残す
- ナビ・絞り込みは `<form data-requires-control>` の外。E2E で「操作権なしでも絞り込める」
    ことをピンした
- 実ブラウザ E2E（`TestSettingsOverBrowser`）が既定非表示セクションの input を直接
    掴んでいたので、ナビを押してから触るよう直した（`_open_settings_section`）
- `make format` の docformatter が日本語 docstring の段落を再折り返しして文を壊すため、
    文ごとに空行を挟んだ（memory の docformatter 項の運用）

## 自己レビューでの修正

- ナビの選択状態を DOM（`aria-current`）から読んでいたのを JS 内の変数へ移した。
    絞り込み中は全 item から `aria-current` が外れるので、絞り込み解除時に選択が
    先頭セクションへ飛んでいた
- `hashchange` を拾うようにした（戻る / 進むでセクションが変わる）
- `config_store.MACHINE_FIELDS` に「表示構成は frontend の `SETTINGS_SECTIONS`」と註記

## code-reviewer の指摘と対応（verdict: request-changes）

- **M1 sticky ナビが topbar の裏に潜る**（must-fix）: `top: 0.75rem` → `4rem`。`.topbar` は
    sticky / `min-height: 3.25rem` / `z-index: 20` で不透明。既存の `.job-completion-notice`
    と同じ基準に揃えた
- **S1 placeholder への実効値表示**: **取り下げた**。ユーザー要求 4 点のどれにも紐づかず、
    AGENTS.md 開発原則 2 / 3（要求されていない機能を足さない・diff をトレース可能に保つ）に
    反する。ページに実効値が出ないのは改修前からの状態で、悪化はしていない。
    「未記載キーの実効値を見せる」は独立した要求として別 PR にする
- **S2 float_pair の placeholder が X / Y を潰す**: S1 の取り下げで解消
- **S3 絞り込み E2E の空振り**: 初期表示セクション内の非マッチ項目
    （`paste_dispenser.nozzle_diameter`）が消えることを見るよう変更
- **S4 JS バグ 2 件の回帰テスト**: `test_section_selection_survives_filtering_and_history`
    を追加（絞り込み解除で選択が戻る / 戻る操作でセクションが切り替わる）
- **S5 カードの折りたたみを外した判断**: 意図的。1 画面 1 セクション + 絞り込みで縦の圧は
    減っており、折りたたみ状態という覚えておけない UI 状態を足す理由が無い。必要になったら
    `<details>` へ戻せる（カード 1 枚が 1 要素なので構造は変わらない）
- nit（`field_count` のトートロジー）: 合計と `MACHINE_FIELDS` 件数の一致に変更。
    他の nit（履歴の積み上がり・絞り込み中のバッジ・walrus のスコープ）は現状維持
