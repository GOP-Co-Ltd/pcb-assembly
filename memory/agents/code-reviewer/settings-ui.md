# 設定ページ 2 ペイン化 レビュー

## verdict: request-changes

must-fix 1 件（sticky ナビが topbar の下に潜る）。仕様準拠・データ整合・DOM 契約は問題なし。

## must-fix

### M1. `.settings-nav` の sticky が topbar に隠れる（ナビ先頭項目がクリック不能になる）

- 対象: `src/web/ui/static/app.css:626-632`（`.settings-nav { position: sticky; top: 0.75rem }`）
- 問題: `.topbar` は `position: sticky; top: 0; min-height: 3.25rem; z-index: 20` で不透明背景（`app.css:128-141`）。`.layout` は `overflow` を持たない（`app.css:301-305`）のでスクロールコンテナは文書そのもの。したがって `.settings-nav` は viewport 上端から 0.75rem に貼り付き、y=[0.75rem, 3.25rem] の帯が topbar の裏に入る。`.settings-nav` は z-index 未指定（auto）なので topbar（20）が上に描かれ、先頭項目「ペーストディスペンサー」（高さ約 2rem）はほぼ全体が隠れてクリックできない。
- 再現: デスクトップ幅で `/settings` を開き、`paste_dispenser`（カード 12 枚）を数画面スクロールする → 左ナビが貼り付いた時点で先頭項目が topbar の下に消える。
- 根拠: 同リポジトリで「topbar の下に置く」既存要素は `top: 4rem`（`.job-completion-notice`, `app.css:884-886`）。`top: 0.75rem` はこの慣習から外れている。
- 備考: 狭幅の `@media` 分岐では `position: static` に戻しているのでモバイルでは起きない。
- 確信度: 高（CSS 上の機構は確定）/ 深刻度: 中（見た目ではなく操作不能）

## should-fix

### S1. placeholder に実効値（`resolved`）を出す変更がユーザー要求からトレースできない

- 対象: `src/web/ui/templates/settings.html:16, 44`、`tests/web/ui/test_pages.py::test_settings_page_shows_effective_values_as_placeholders`
- 問題: ユーザー要求 4 点（階層化 / ディスペンサー分裂 / 並び替え / 2 ペイン化）のいずれにも対応しない機能追加。orchestrator ノートが「現状の問題」として自ら足した項目。
- 根拠: AGENTS.md 開発原則 2「要求されていない機能を追加しない」/ 3「diff の各行をユーザー要求へ直接トレースできる状態にする」。
- 書き戻し事故は無い（`scalarValue` は `input.value` しか読まない。`settings.js:25`）ので動作上は安全。採否は orchestrator 裁定（残すか別 PR へ切るか）。
- 確信度: 高 / 深刻度: 中

### S2. `float_pair` の placeholder が X / Y の視覚的手掛かりを潰す

- 対象: `src/web/ui/templates/settings.html:16`
- 問題: `placeholder="{{ field.resolved[loop.index0] if field.resolved is not none else axis }}"`。`reference_point.offsets.*` 4 項目はいずれも既定値を持つので `resolved` は通常 非 None。その結果、従来 "X" / "Y" と出ていた 2 つの入力が両方とも数値 placeholder になり、どちらが X でどちらが Y か視覚的に判別できない（`aria-label` にしか残らない）。
- 併せて `field.resolved[1]` は resolved が長さ 2 の list である前提。`_as_setting_value`（`src/web/api/routers/common.py:284-287`）は tuple をそのまま list 化するだけなので、将来 2 要素でないタプルが来ると Jinja が IndexError → 500（長さ仮定の部分は 確信度: 低 / 現状のデータでは起きない）。
- 確信度: 高（X/Y 喪失）/ 深刻度: 低〜中

### S3. E2E の「絞り込みが横断する」テストに空振りアサーションがある

- 対象: `tests/e2e/test_browser_ui.py::TestSettingsOverBrowser::test_filter_reaches_settings_of_other_sections_without_control`
- 問題: `input[name="camera.width"]` が hidden であることを確認しているが、`camera` は `SETTINGS_SECTIONS` の 4 番目で **初期表示から hidden**。絞り込みが一切動作しなくても通る。「絞り込みで非マッチが消える」を示すなら、初期表示セクション（`paste_dispenser`）内の非マッチ項目（例 `paste_dispenser.nozzle_diameter`）が hidden になることを見るべき。
- `probe.min_samples` が visible になる側のアサーションは有効（probe は既定非表示）。
- 確信度: 高 / 深刻度: 低（テストの証明力のみ）

### S4. 自己レビューで見つけた 2 件の JS バグに回帰テストが無い

- 対象: `src/web/ui/static/js/settings_nav.js`、`tests/e2e/test_browser_ui.py`
- 問題: orchestrator ノート「自己レビューでの修正」の 2 件（(a) 絞り込み解除時に選択セクションが先頭へ飛ぶ、(b) `hashchange` を拾う）が、どちらもテストでピンされていない。E2E が観測できる振る舞い（skill testing-strategy の e2e 区分）なので 1 本ずつ足せる:
    - 絞り込み → 入力クリア → 元の選択セクションが復帰する
    - `goto(".../settings#camera")` → camera パネルが開く
- 確信度: 高 / 深刻度: 中（同じバグが再発しても全緑）

### S5. カードの折りたたみ（`<details open>`）が予告なく失われた

- 対象: `src/web/ui/templates/settings.html:76`（`<details class="settings-group" open>` → `<section class="settings-group">`）
- 問題: 従来はカード単位で開閉できた。`paste_dispenser` は 12 カード / 49 項目なので、畳めないのは縦方向の圧を戻す方向。要求にも計画書にも「折りたたみを廃止する」判断は書かれていない。2 ペイン化で不要と判断したなら計画書にその旨を残すべき。
- 確信度: 中（意図的な可能性が高い）/ 深刻度: 低

## nit

- `tests/web/ui/test_layout.py::TestSettingsSections::test_every_machine_field_is_rendered_once` 末尾の `[section.field_count ...] == [sum(len(group.fields) ...)]` は `field_count` の実装をそのまま再計算した比較で、実装が変われば両辺とも変わるトートロジー（限界価値テスト、skill testing-strategy）。
- 同ファイル `test_no_group_is_empty` も宣言の自明な性質。残すなら「空カードが描かれない」という描画側の振る舞いに寄せたほうが価値がある。
- `test_settings_page_shows_effective_values_as_placeholders` は fixture の machine.toml に未記載キーが 1 つも無くなると `next()` が StopIteration で落ちる（skip や明示 assert でないので原因が読み取りにくい）。また `placeholder="{resolved}"` の生文字列比較なので、resolved に HTML エスケープ対象文字が入ると falsely fail する。
- ナビのバッジ（`field_count`）は絞り込み中も総数のまま。ヒット数に追随しない。
- `applyFilter` が全 item から `aria-current` を外すため、絞り込み中は「今どのセクションに戻るのか」が画面から消える（`settings_nav.js:39`）。
- ナビクリックが `location.hash = activeSlug`（`settings_nav.js:63`）なので、セクションを切り替えるたび履歴が積まれ、ページを離れるのに Back 連打が要る。
- `#settings-filter-empty` が `.settings-layout` の外にあるため、0 件時にメッセージがナビ・パネル領域の下に出る（`settings.html:97`）。
- `settings_sections()` の genexp 内 walrus `present` は関数スコープへ漏れる（`layout.py:536-540`）。動作は正しいが読み手を止める。
- `pages.py` のコンテキスト名 `settings_sections` が `layout.settings_sections`（関数）と同名。
- SSR は `loop.first` に `aria-current` を置くので、`#camera` 付きで開くと JS 実行までの一瞬だけ先頭セクションが見える（`settings_nav.js` は body 末尾なので実害はほぼ無い）。

## 確認して問題なかった点（依頼で名指しされた箇所）

- **設定が画面から消える経路**: 無い。`settings_sections()` は宣言漏れを `_uncategorized_section` へ落とし、`test_every_machine_field_is_declared_exactly_once` が `MACHINE_FIELDS` との双方向・重複なしをピンしている。存在しないキーの宣言（静かな欠落）も同テストが弾く。
- **`settings.js` の DOM 契約**: `name` / `data-type` / `data-allow-empty` / `.settings-pair[data-pair-key]` / `input[data-pair-index]` / `form[data-endpoint]` はすべて維持。`#settings-filter` はフォーム外かつ `name` 無しなので誤保存に乗らない。`hidden` は `input`/`change` の発火に影響しない。
- **ナビ・絞り込みをフォーム外に出した判断**: `control.js` の `render()` は `[data-requires-control]` の要素にだけ `inert` を付ける。`base.html` の `<main>` や `<section class="settings">` に `data-requires-control` は無いので、ナビと絞り込みは閲覧専用端末でも操作できる。E2E でピン済み。
- **placeholder による machine.toml への書き戻し事故**: 無い。`scalarValue` / `pairValue` はどちらも `input.value` のみを読み、空欄は `null`（= 送らない）。placeholder は値に昇格しない。
- **`hidden` の効き**: `.settings-group` / `.settings-section` / `tr[data-search]` に `display` を上書きする CSS は無い。ナビ項目は `<button>`（`display: inline-flex`）だが `hidden` ではなく `.is-empty` クラスで表現しているので `button[hidden]` の罠を踏んでいない。
- **成果物汚染**: 変更ファイルに `</content>` 等の混入なし。

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（3342 passed, 183 deselected / 139.5s）
- 実機テストは未実行（規約どおり）。ブラウザ実描画の確認（M1）はユーザー側で。
