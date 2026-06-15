# WebUI Phase 4 — pad 編集フロント UI（実装ログ）

計画書: `/home/gop/.claude/plans/claude-webui-1-pad-extract-eager-pine.md` Phase 4。
ブランチ: feature/20260615/paste-frontend。Phase 1/2/3（pcbasm 純ロジック +
applicator override + webui 永続化 + pad-config API）は実装済み。

spec-test-author の engagement: なし。本 agent が実装 + ページ DOM テスト両方を担当。
Phase 3 の API 契約（`src/webui/routers/pasting.py`）は確定済みで、フロントは
それを GET/PATCH で消費するのみ（IF 変更なし）。

## 新規/変更ファイル

新規:
- `src/webui/templates/pasting/paste_solder.html`（pasting/job.html を土台に
  見出し直後へ pad_editor を include）
- `src/webui/templates/partials/pad_editor.html`（レイヤ切替・SVG・選択ツール
  バー・階層表コンテナ・未選択メッセージ + pad_editor.js）
- `src/webui/static/js/pad_editor.js`（SVG ビューア・クリック/ドラッグ選択・
  一括適用・階層 override 表・ビューア⇄表連動・ジョブ実行中ロック）

変更:
- `src/webui/routers/pages.py`: `FEATURE_TEMPLATES[("pasting","paste_solder")]` を
  `"pasting/paste_solder.html"` に、`_JOB_TEMPLATES` に同テンプレを追加。
  既存の `_PASTING_PREVIEW` / `_PASTING_LOADING_PARAM` に paste_solder が
  含まれるので show_preview / show_loading_controls はそのまま効く。
- `src/webui/static/app.css`: pad-viewer / pad-table のスタイル追記。
- `tests/webui/routers/test_pages.py`: `TestPasteSolderPadEditor` 追加。

## API 契約からの差異

なし。Phase 3 の GET/PATCH をそのまま消費。node_id 規約・pad id 規約・
affected_pads 形状・409（PCB 未選択）を厳守。

## 計画外の判断ログ

1. **enabled チェックボックスを三状態（indeterminate）で表現**。L0 以外で明示
   enabled が無い（継承）行は `indeterminate=true` で「継承」を視覚化し、別途
   「継承」ボタンで `enabled:null` を送って継承に戻す（計画の「任意: 三状態 or
   別ボタン」のうち両方を軽量に併用）。L0 は継承元が無いので indeterminate 化
   しない（API も L0 の enabled=null を 400 で拒否する契約）。

2. **継承セルの placeholder は client 側で祖先チェーンを合成して解決値を算出**。
   GET は per-pad の resolved と per-node の疎 override しか返さないため、node 行の
   「解決された継承値」は overrides マップを L0→node のチェーンで上書き合成して
   求める（tree から parentOf を構築）。pad 解決と同じ「最具体の明示値が勝つ」
   セマンティクスを踏襲。

3. **PATCH 応答後にローカル overrides マップを楽観更新**（再 GET しない）。
   affected_pads は pad の enabled/resolved を更新するが、node 行の override
   表示（●/×/濃淡）はローカル overrides の更新が必要なため `updateLocalOverride`
   で body を反映してから表を再描画する。clear で空 + enabled 継承になった
   ノードは overrides から削除（L0 は常に保持）= API の疎表現と一致。

4. **空入力 = clear（継承に戻す）扱い**。継承セルに何も入れず blur した場合は
   no-op。override セルを空にして blur すると clear を送る。

5. **ロック制御は renderTable 内で一括 disable + onUpdate 遷移時のみ再描画**。
   `window.webui.jobs.onUpdate` で非終端 status を locked とし、解除時は
   renderTable で行ごとの本来の disabled（継承ボタン等）を復元する。
   TERMINAL に "idle" も含める（job=null や idle は非ロック）。

6. **矩形ドラッグの交差判定は SVG getBBox（mm 系 bbox）と選択矩形の AABB**。
   クリック/ドラッグの分岐は移動量 0.3mm 閾値。pointer capture で SVG 外へ出ても
   追従。Shift=追加 / Alt=除外 / 修飾なし=置換。現レイヤの pad のみ対象。

## 実ブラウザ未検証の範囲

SVG クリック/ドラッグ選択・表セル編集・行 hover ハイライト等の実ブラウザ操作は
Claude が検証できない（skill webui-e2e の制約）。curl では GET 200 + DOM/属性/
script タグ + API 応答（affected_pads）まで確認する。

## 検証結果

- make format: pass（全 pre-commit フック Passed）
- make type: pass（pyright エラー 0 / 警告 0）
- `uv run pytest tests/webui -q -m "not hardware"`: 403 passed, 18 deselected
  - 新規 TestPasteSolderPadEditor 含む TestPastingJobPages 系: 28 passed
- 実機を要する hardware mark テストは未実行（memory 規約）。

## curl 実サーバ E2E（make webui-fake 相当・port 8099・isolated data_dir）

設定書き込みは test-fixture マシン。data_dir=/tmp/pcbasm-webui-fake-padfront。
リポジトリ configs への書き込みなし（git status クリーン）。

- `GET /pasting/paste_solder` 200。DOM フック全 FOUND（pad-viewer / pad-table /
  pad-enable-selected / pad_editor.js / name="pad-layer" / pad-editor-empty）+
  job-console / job-form / preview-pane / loading-controls。script タグに
  app.js（webui.api/toast）/ job_console.js（jobs.onUpdate）/ pad_editor.js。
- PCB 未選択時 `GET /api/pasting/pad-config` 409。
- 実 PCB（data/testing/led_blinker）選択後 GET: outline 5pt(20x25mm) / 18 pads
  （polygon は outline bounds 内、Top/Bottom 両層）/ defaults 7 項目 / tree
  L0→L1×5 package / overrides={L0}。
- `PATCH .../pads {ids:[U1.1,U1.2],enabled:false}` → affected で enabled 反映。
- `PATCH .../node {L2:U1, values:{bead_width_factor:0.8}}` → U1 配下 6 pad の
  resolved.bead_width_factor=0.8。
- `PATCH .../node {L2:U1, enabled:false}` → U1 配下全 disabled。
- `PATCH .../node {L2:U1, clear:[bead_width_factor]}` → resolved が L0 既定 1.0 へ。
- 再 GET で永続化確認（U1.1 と U1.3 が disabled、overrides に L4:U1:1/L4:U1:2/
  L2:U1 が残存）。board_settings JSON が isolated data_dir に書かれた。
- エラー: 未知 field / 未知 node / L0 enabled=null が全て 400。
- `POST .../reset` → overrides={L0}・全 pad enabled に復帰。

## 実ブラウザ未検証（要ユーザー）

SVG クリック/矩形ドラッグ選択（Shift/Alt 修飾）・表セル編集の blur/Enter・
行 hover による pad ハイライト・pad 選択 → L4 行 scrollIntoView・ジョブ実行中の
編集ロックの体感は Claude では検証不可（DOM/属性/ハンドラ装着と API 応答までを
curl で確認済み）。
