# はんだ塗布への運転時流量キャリブレーション組み込み レビュー

ブランチ `feature/2026-09-11/paste-solder-flow-calibration`（未コミット、`git diff main`）。
レビュー時点でまだ編集が続いている（レビュー中に `src/web/ui/static/app.css` の
`.paste-flow-calibration` スタイルが増えた）。以下はその時点のツリーに対する判定。

## verdict: request-changes

## must-fix

### 1. はんだ塗布ページで校正ファイルを選んでも保存されない

対象: `src/web/ui/templates/pasting/paste_solder.html:94-95` /
`src/web/ui/static/js/paste_volume_calibrations.js`（`swap`）/
`src/web/ui/static/js/settings.js:108-112`

`settings.js` は読み込み時に `machineForm.querySelectorAll("input, select")` で
**その瞬間に存在する要素へ直接** `input` / `change` を貼る。
`paste_volume_calibrations.js` の `load()` は `await api("GET", ...)` を挟むので、
`input.replaceWith(select)` が走るのは fetch 解決後 = `settings.js` の実行後。
生成された `<select>` にはリスナーが 1 つも付かない。

結果、校正ファイルの `<select>` を操作しても `PUT /api/settings/machine` が飛ばない。
`data-allow-empty` で入れた「補正しない」（空文字送信）も同じ経路なので効かない。

再現: paste_solder ページ →「流量キャリブレーション」の校正ファイルを選択 →
machine.toml が変わらない（リロードで戻る）。校正一覧の fetch が失敗したときだけ
元の `<input>` が残るので手入力は保存される＝正常系だけが黙って壊れる。

根拠: 要求 4「使う校正ファイルも machine.toml に設定可能」/ 要求 5「はんだ塗布の
ページからも設定可能」。classic script は順に同期実行され、`api` は
`src/web/ui/static/js/app.js:50` の async fetch。

確信度: 高（コード上は決定的。ブラウザ実機は未確認）
深刻度: 高（機能の主入口が無言で無効）

### 2. `_flow_calibration_label` が誤った理由を表示する

対象: `src/web/api/routers/pasting_view.py:489-498`

`plan is None` になる原因は 3 通り（校正ファイル未設定 / `point_count == 0` /
配置エラー）あるが、配置エラーが最後の else に落ちて
「測定点数 0 のため補正しません」になる。

再現:

```
cfg = FlowCalibration(calibration_file='cal.json', point_pitch_mm=200.0)  # point_count=3
plan, err = plan_flow_calibration(config=cfg, point=Point2d(10,4), outline=40x30矩形)
# err  = 流量キャリブレーションの測定点が基板外形の外に出ます: (210.000, 4.000)
# label = (10.00, 4.00) mm（測定点数 0 のため補正しません）
```

`pad_editor/index.js` は `selection_label` を本文に、`error` は `title` 属性にしか
出さないので、運転者が読む文言が事実と食い違う。
`tests/web/api/routers/test_pasting.py::test_a_row_running_off_the_board_is_reported_without_failing`
がまさにこの状態を作っているが label を検証していない。

確信度: 高（再現済み）
深刻度: 中（誤った原因表示で運転者が point_count を疑う）

### 3. `crop_size_mm` を単独で上げると machine.toml が読めなくなる

対象: `src/pcbasm/config.py`（`FlowCalibration.__attrs_post_init__` の
`point_pitch_mm > crop_size_mm`）vs `src/web/api/config_store.py::write_machine_settings`

`_coerce` は 1 フィールドずつしか検証せず、書き込みは tomlkit へ直接なので、
cross-field 不変条件を壊す値がそのまま永続化される。

再現（実行済み）:

```
ConfigStore(tmp).write_machine_settings({'paste_dispenser.flow_calibration.crop_size_mm': 4.0})
# -> write OK
Machine(tmp/'machine.toml').paste_dispenser
# -> ClassValidationError While structuring PasteDispenser
```

既定は crop 2.0 / pitch 3.0 なので「crop を 4 mm にしたい」だけで 1 操作で踏む。
以後 `machine.paste_dispenser` を読む経路（pad-config API、塗布ジョブ）が落ちる。
設定画面は `_resolved_values` が例外を握り潰すので復帰は可能だが、はんだ塗布ページの
pad editor は 500 になる。

確信度: 高（再現済み）
深刻度: 中〜高（復帰可能だが装置が使えなくなる）

## should-fix

### 4. `RELIABLE_RANGE_MARGIN` が被覆域の幅に対する相対値

対象: `src/pcbasm/pasting/paste_volume/model.py`（`RELIABLE_RANGE_MARGIN = 0.35`）

この MR で追加した `data/paste-volume-calibrations/...20260911T104851...json` は
被覆域 0.334–1.046 mm なので `reliable_diameter_min_mm = 0.583 mm`。
コメントと docs が約束する「下限が 0.79〜0.83 mm」は 9/10 の 2 校正
（0.642–1.186 / 0.613–1.125）にしか成り立たない。
0.583 mm は docs の表で食い違い 58 % と書かれた側に近く、狙った保証が出ていない。
絶対値の下限（または校正ごとに同定性から決める量）にするか、docs の記述を
「幅の 35 % を落とす」という事実だけに留めるかのどちらか。

確信度: 高（算術は検証済み）/ 中（どちらの直し方が正しいかは設計判断）

### 5. 既定 `crop_size_mm = 2.0` がどの校正の crop とも一致せず、警告もされない

対象: `src/pcbasm/config.py`（既定値）/ `src/pcbasm/pasting/paste_volume/calibration.py::mismatches`

保存済み 3 校正の `crop_size_mm` は 1.8 / 1.8 / 2.5。既定 2.0 はどれとも違う。
`detect.py` は crop 内の Otsu と 99 % 分位点で 2 値化するので、crop の広さは
背景／前景の画素比を通じて閾値に効き、面積等価直径が数 % 動きうる。
補正したい量そのものと同じオーダー。
`mismatches()` はノズル径・塗布高さ・pixel_per_mm は見るのに crop は見ない。
crop を比較項目へ足すか、既定を校正の `crop_size_mm` に寄せる運用を docs に書くか。

確信度: 中（影響量は未測定。crop が Otsu に効くこと自体は確実）

### 6. 上限側の余裕が小さく、過吐出ほど不採用になりやすい

対象: `src/pcbasm/pasting/paste_volume/estimator.py`（上限は被覆域のまま）

新校正で 0.2 μL は直径 ≈0.957 mm、`diameter_max_mm` は 1.046 mm（V=0.263 μL）。
つまり +32 % を超える過吐出は `diameter_out_of_calibrated_range` で落ちる一方、
過少側は V(0.583)=0.039 μL（−80 %）まで拾う。
いちばん補正したいケースが不採用になる非対称がある。
docs の「0.2 μL は十分内側」は下限側にしか当てはまらない。

確信度: 高（算術）/ 中（許容するかは設計判断）

### 7. `FlowCalibrationInfo.points` を誰も使っていない／測定点が図に出ない

対象: `src/web/api/routers/pasting_view.py::build_flow_calibration` /
`src/web/ui/static/js/pad_editor/viewer.js`

`points`（実際に塗る点列）を API が返すが、JS は `selection_label` / `error` /
`point` しか読まない。初回パージは `renderPurgeMarker` で SVG にマーカーが出るのに、
流量キャリブレーションの起点も並びも図に出ない。
起点しかクリックで選べないのに 2 点目以降は +X に自動で並ぶので、
パッドやパージ点に重なっていないかを運転者が確認できない。
描くか、使わないなら `points` を返さない（要求外の出力を残さない）。

確信度: 高

### 8. `.pad-flow-calibration-*` の CSS が無い

対象: `src/web/ui/templates/partials/pad_editor.html` / `src/web/ui/static/app.css`

`.pad-initial-purge-tools` / `-actions` には grid 定義があるが、
新設の `.pad-flow-calibration-tools` / `-point` / `-actions` には 1 行も無い。
すぐ上のパージブロックと見た目が揃わない。

確信度: 高（レビュー中に `.paste-flow-calibration` 側だけ追加されたので、対応中の可能性）

### 9. `web/api/jobs/pasting/flow_calibration.py` にテストが 1 件も無い

`tests/web/api/jobs/test_pasting.py` は他のジョブ層ヘルパ（`_save` など）を
直接呼んでテストしているので、この層をテストしない理由が無い。
とくに固めるべき契約:

- 校正が読めない / crop が決まらない / 採用 0 件 → `None` を返しジョブを落とさない
- 撮影 3 パスの順序（全点 pre → 全点塗布 → 全点 post）
- `summary_line` の内容（`JobResult.summary` に載る）

確信度: 高

### 10. 一般設定ページでは校正ファイルを空に戻せない

対象: `src/web/ui/templates/settings.html`（`settings_groups` マクロ）

`data-allow-empty` は paste_solder テンプレートにしか無い。
/settings 側の同じ項目は空欄にしても「未入力＝保存しない」のままで、
無言で何も起きない。空文字が意味を持つ唯一の項目なので入口で挙動が割れる。

確信度: 高

### 11. `correction_for` の ValueError が `run_flow_calibration` から漏れる

対象: `src/web/api/jobs/pasting/flow_calibration.py::_capture_all`
（`correction.alignment.correction_for(point)`）

`posctrl/alignment.py:58-73` は成功領域が 1 件も無いと `ValueError` を投げる。
モジュール docstring の「補正できないことでジョブを落とさない」に反する経路。
実際には同じ状態なら pad 塗布側でも落ちるので実害は小さいが、
初回パージが無効（`initial_purge_ul = 0`）のときは流量キャリブが最初の犠牲者になり、
失敗フェーズの表示が「塗布」ではなく「流量キャリブレーション」になる。

確信度: 高（機構）/ 低（実害）

## nit

- `fit.py::_default_label` の docstring に二重スペース（`なり、 WebUI`）。
  docformatter の行結合の跡と思われる。
- `evaluate.condition_mismatch` の文言が "session X px/mm" → "現在 X px/mm" に
  変わっている。共有化の副作用で、既存の校正検証レポートの表示を変えている
  （要求外の変更）。
- `auto_calibration_path` に単体テストが無い。
  `tests/.../test_calibration.py` は `calibration_filename` / `calibration_path` を
  細かく固めているので、「自動命名では時刻を重ねない」契約もそこで固めたい。
  既存の `test_an_empty_save_name_still_saves_under_an_auto_name` は
  `startswith("paste-1-n0.34-h0.20-")` しか見ておらず、二重時刻でも通る。
- 測定点と初回パージ点・実パッドの衝突を誰も検証しない。
  pre 画像にパージ痕が写ると最大連結成分が別物になる。
- docs の「`point_pitch_mm` は `crop_size_mm` より大きくする」に、
  実装が `point_count > 1` のときだけ強制する例外が書かれていない。

## 良かった点（確認済み・指摘なし）

- `PointCapturer` の `correction` は `camera_point_target`（ステージ）と
  `CopperProjector.with_correction`（board→pixel affine）の両方に効いており、
  `tests/pcbasm/pasting/test_capture.py::TestPointCapturerWithAlignmentCorrection`
  が「補正しても crop は画面中央のまま」で両者の一致を固めている。
- `adopt_rotations_per_ul` は `make_applicator` が毎回新しい `PasteDispenser` を
  作るのでジョブ内に閉じており、`rotations_per_ul` の参照は全て
  `self._dispenser` 経由（`applicator.py:367`、`pushpull` の μL→deg 変換）なので
  差し替えが取りこぼしなく効く。AirPump を回さない設計判断も妥当。
- `reliable_range_only` の既定 `False` で従来経路（fit / evaluate）の振る舞いは
  変わっていない（`test_estimator.py::TestReliableRangeOnly` が両側を固めている）。
- pad editor の点設定モードは双方向に排他（`setPurgePointMode` /
  `setFlowCalibrationPointMode` が互いを false にし、相手の render も呼ぶ）。
- `</content>` 等の混入なし（src / tests / docs / memory を grep）。

## 検証結果

- make format: pass
- make type: pass（0 errors, 0 warnings）
- pytest -m "not hardware"（tests/pcbasm/pasting, tests/pcbasm/test_config.py,
  tests/web/api/routers/test_pasting.py, tests/web/api/test_config_store.py,
  tests/web/api/test_board_settings.py, tests/web/ui）: 1812 passed
- 実機テストは未実行（禁止）

## 補足

レビュー中に `git stash` を一時使用したため index が解除された。`git add -A` で
全ファイル staged に戻してある（元は `memory/agents/orchestrator/...md` だけ
`AM` だったのが `A ` になっている点だけ差分）。
