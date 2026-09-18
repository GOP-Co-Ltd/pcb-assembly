# はんだ塗布タクトタイム表示 レビュー

## verdict: request-changes

事後（実測）側は要求どおり動く。事前（予測）側は「動く」が、移動時間モデルが
リポジトリ内の printer.cfg と突き合わせると系統的に 3〜4 割短く出る。M1/M2 を直せば approve。

## must-fix

### M1. Z 移動（下降・上昇）を XY の速度・加速度で見積もっている / 確信度: 高 / 深刻度: 高

- 対象: `src/pcbasm/pasting/tact.py` の `lift_sec = trapezoidal_time(settings.lift_height,
  tact.travel_speed, tact.travel_accel)`
- `to_gcode` の step 2（塗布高さへの下降）と step 6（上昇）は純 Z 移動。Klipper の cartesian
  kinematics は Z 成分を含む move を `max_z_velocity` / `max_z_accel` で clamp する
  （`z_ratio = move_d / |axes_d[2]|` なので純 Z 移動は等倍で効く）。`[tact]` は XY しか持たない。
- 根拠（リポジトリ内で検証可能）:
    - `data/config-templates/kurousagi.paste/printer.cfg:17-20`
      max_velocity 50 / max_accel 250 に対し **max_z_velocity 5.0 / max_z_accel 15**
    - `data/config-templates/usaremino.paste/printer.cfg:18-21`
      10 / 50 に対し **max_z_velocity 5 / max_z_accel 10**
    - kurousagi の `lift_height = 2.0` で: 見積り `sqrt(2*2/250) = 0.127 s`、
      実際 `2*(5/15) + (2.0-1.667)/5 = 0.733 s` → **5.8 倍の過小評価**
    - 1 成分あたり: 下降 +0.61 s、上昇 `max(retract 0.25, lift)` が 0.25→0.733 で +0.48 s
      → **約 1.09 s 取りこぼす**
    - 同 config の点塗布 pad（0.72 mm²）は `dispense_duration ≈ 1.35 s`。
      pad 1 枚 見積り約 1.9 s に対し実際約 3.0 s（**約 -38%**）
- 方向: `[tact]` に `z_speed` / `z_accel`（printer.cfg の max_z_velocity / max_z_accel を写す）

### M2. `trapezoidal_time` に減速区間が無い / 確信度: 高 / 深刻度: 中

- 対象: `src/pcbasm/pasting/fill_sequence.py` の `trapezoidal_time`、`tact.py` の travel/lift 算出
- この関数は「0 から加速 → rate で巡航」だけで終端速度が 0 に戻らない。吐出（stepper が
  止まらず次動作へ繋がる）には正しいが、`wait_for_done` / `G4 P0` で必ず停止するステージの
  点間移動には合わない。
    - 短距離 `d <= rate^2/2a`: 実際 `2*sqrt(d/a)` に対し見積り `sqrt(2d/a)` → **1.41 倍の過小**
    - 長距離: 実際は見積り + `rate/(2a)`
- 公開化にあたり docstring を「吐出とステージ移動の両方が使う」と書き換えて共有を正当化して
  いるが、必要なプロファイルが違う。名前も `trapezoidal_time` だが台形ではない。
- M1 と同根なので移動時間モデルとしてまとめて直してよい。

## should-fix

- **S1** `per_component_ul` の二重化 / 確信度: 高。`tact.py` と `applicator.py` の
  `polygon.area * params.ul_per_mm2 / len(plan.paths)` が同一式。`FillPlan` 側に 1 つ生やす。
- **S2** 見積り API のコスト / 確信度: 高（実測）。面塗布 pad 1000 枚で 1.0 秒。
  `pad_editor` が編集のたび `webui:pad-config-changed` を出すので 1 編集ごとに CPU 1〜数秒。
  `state_changed` はファイルアップロード・ノズルキャップ操作など無関係な経路からも飛ぶ。
  JS 側 debounce か、`build_fill_path` 同様オンデマンド化。
- **S3** `build_tact_estimate` docstring「実行時と同じ関数を通す」が不正確 / 確信度: 中。
  `plan_paste_targets` は `pcb.pads` 起点、`build_tact_estimate` は `hierarchy.iter_pads()` 起点。
  階層外 pad は実行時は既定 params で塗られるが見積りから落ちる。KiCad 読込では両者が同じ
  footprint 列由来なので実害は薄い。docstring 修正だけでも可。
- **S4** `[tact]` のドキュメントが無い / 確信度: 高。`data/config-templates/README.md` は
  `[audio]` を任意セクションとして説明しているのに `[tact]` は無い。printer.cfg はリポジトリ
  追跡外なので、実機の max_velocity を変えても `[tact]` が追随せず誰も気付かない。
- **S5** `TactEstimateResponse` の置き場所 / 確信度: 高。`# 共通: PCB / 階層 / モデルのロード`
  バナーの直下に入っていてセクションの意味が崩れている。
- **S6** 実測値がどこにも残らない / 確信度: 中。`elapsed_seconds` はブラウザ表示のみで
  `JobResult.summary` にもアーティファクトにも入らない。`setup_sec` を実測で調整する前提
  （`config.Tact` docstring）なのに、その実測値を後から参照できない。
- **T5** テストが単調性しか見ていない / 確信度: 高。`test_single_dot_pad_...` は `start=dot` で
  travel を 0 にしているため、移動時間の式が値として一度も固定されていない。M1/M2 のような
  モデル誤りがあってもテストは落ちない。

## テストの抜け

- T1 `estimate_paste_tact` と `PasteApplicator.apply` の一致（成分への量の割り振り）が未固定
- T2 `plan.paths` 空 pad で `pad_count` だけ増える分岐が未カバー
- T3 見積りの pad 選別が `plan_paste_targets.routed_pads` と一致することが未検証
- T4 `formatDuration` の JS テスト無し（JS テスト基盤が無いので実質 nit）

## nit

- `start=Point2d(0,0)`（board 原点）は実際の塗布ループ開始位置（パージ点直後）と違う。1 回ぶん
- `line_reference` 未指定で線塗布 pad の始点終点が実行時と入れ替わりうる。経路長は不変
- 計画メモの `estimate_paste_tact(pads, params_for, ...)` が実装（`targets`）と食い違ったまま
- `JobSummary.elapsed_seconds: float = 0.0` は他の任意フィールド（`| None = None`）と慣例が違う。
  旧 backend と組むと JS の `?? 0` で 0 から数え始めた偽の経過が出る
- `_park_machine` / 通知音は `finish()` の後なので実測に入らない
- `tact_estimate.js` の refresh 契機が `job_console.js` の WS 再放送に依存する暗黙結合

## 意図的判断への裁定

1. **formatDuration を JS 1 箇所** → 妥当。経過時間はクライアントが毎秒進めるので整形を
   サーバーに置けない。サーバーは秒しか返さず JS はサーバー値を再導出していないので
   webui-thin-wrapper の核心は守られている
2. **初回パージ・流量キャリブを setup_sec に含める** → 妥当。ただし既定 300 s の根拠が無く、
   実測で合わせる前提なのに実測値が残らない（S6）
3. **`[tact]` に printer.cfg を写す** → 方針は妥当（接続非依存）。ただし XY しか写していないのが
   M1。この設計を貫くなら max_z_velocity / max_z_accel も写す。ドキュメント化も要る（S4）

## 検証結果

- make format: pass
- make type: pass
- make test-no-hardware: pass（3374 passed, 184 deselected）
- 成果物汚染（`</content>` 等の混入）: 無し

______________________________________________________________________

# 2 巡目レビュー（1 巡目指摘への対応の検証）

## verdict: approve

must-fix は無し。M1 / M2 の修正はいずれも妥当で、新たなバグは見つからなかった。
以下は nit / 軽微な should-fix のみ。

## M1 / M2 対応の検証

- **M1（Z を XY の速度で見積もり）→ 妥当**。`Tact.z_speed` / `z_accel` を追加し、
    下降・上昇を `_move_duration(settings.lift_height, tact.z_speed, tact.z_accel)` で見積もる。
    `to_gcode` step 2 / 6 はどちらも距離 `lift_height` の純 Z 移動なので一致する。
    template の値は printer.cfg と一致を確認（kurousagi 5.0/15、usaremino 5/10）。
    `paste_solder` は `session.make_applicator()` を引数なしで呼ぶので `lift_height` の
    上書きは無く、見積りと実行が同じ値を見る
- **M2（減速区間が無い）→ 妥当**。`_move_duration` の閉形式を検算：
    `ramp = v²/a` は加速 + 減速で使う距離、短距離側 `2√(d/a)`（頂点速度 √(ad)）、
    長距離側 `2v/a + (d − v²/a)/v`。いずれも停止 → 停止の厳密解。
    適用先（XY 移動・下降・上昇・リトラクション）が実際に停止 → 停止であることも確認：
    `_draw_polyline` が成分ごとに `wait_for_done()` を付け、`to_gcode` は下降後に
    `wait_for_done()`、リトラクション前に `G4 P0`、末尾に `sync()` を置く。
    `fill_sequence._trapezoidal_time` は private・元 docstring に戻り、公開参照は残っていない
- S1〜S5 もいずれも対応済み（S4 のみ下記 nit 1 件）

## should-fix

なし

## nit

1. `data/config-templates/README.md:52-53` / 確信度: 高。`\*\*...\*\*` が mdformat に
    エスケープされていて太字にならず、リテラルの `**` が見える。閉じ `**` の直後が
    日本語（`ず`）で CommonMark の right-flanking 条件を満たさないのが原因。
    `docs/paste-volume-diameter-calibration.md:99` と同じく閉じ `**` の後に空白を置く
2. `src/pcbasm/pasting/tact.py` module docstring / 確信度: 中。「ステージ移動…はどれも直前の
    `wait_for_done` / `G4 P0` / `sync` で停止した状態から始まり」は下降だけ当てはまらない。
    `to_gcode` step 1 は XY 移動と Z 下降を連続 queue するので lookahead が junction 速度を
    持ち越す。見積りは過大側（安全側）にずれるだけなので実害は無い
3. `FillPlan.component_amount_ul` の `if not self.paths: return 0.0` / 確信度: 高。
    `applicator.apply` は空 plan を早期 return するので、この分岐は `tact.py` が
    `plan.paths` を見る前に量を計算するためだけに要る。`tact.py` 側で順序を変えれば不要
4. `app.js formatDuration` の `Math.round` / 確信度: 中。毎秒更新 + `job_status` ごとの
    再同期で秒が飛ぶ / 重複することがある。経過時間表示は `Math.floor` が通例
5. `Tact` の既定値（travel 10/50）は usaremino（テスト用 fixture）と同じで kurousagi とは違う。
    `config/machine.toml` に `[tact]` を書き忘れた機体は travel が 5 倍遅い見積りになり、
    警告も出ない。README には書かれているので運用でカバーする前提
6. 1 巡目 T3（見積りの pad 選別が `plan_paste_targets.routed_pads` と一致すること）は
    直接は固定されていない。`test_reports_setup_and_dispense_for_the_selected_board` が
    pad-config の有効 TOP pad 数と突き合わせる形で近いことは見ている
7. 1 巡目 nit の `JobSummary.elapsed_seconds: float = 0.0`（他の任意フィールドと慣例が違う）は未対応

## S6 却下の裁定: 却下を支持する

- 要求は「開始で計り、終了で止める」まで。`JobRecord.elapsed_seconds` は `finish()` で
    凍結し、ページ再読込後も `GET /api/jobs/current` 経由で同じ値が出るので、
    `data/config-templates/README.md` が書いた「コンソールの経過時間と見積りを見比べて
    `setup_sec` を調整する」運用はこのままで回る。S6 の前提（実測が消える）は弱い
- 表記の二重化を避ける理由は副次的だが、AGENTS.md 開発原則 2/3 からも追加実装は不要
- 残る制約はユーザーに伝えるべき: 実測値は次のジョブ開始で失われる（`JobRecord` は
    非永続で直近 1 件のみ）。機体ごと・基板ごとに実測を貯めたくなった時点で別タスクにする

## 検証結果

- make format: pass
- make type: pass
- make test-no-hardware: pass（3378 passed, 184 deselected）
- 成果物汚染（`</content>` 等の混入）: 無し
