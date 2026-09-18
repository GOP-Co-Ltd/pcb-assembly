# ノズルクリーニングを「塗布未検出時のみ」に変更 レビュー

## verdict: request-changes

## must-fix

### M1. `_clean_and_purge` の `retract()` がプライム状態と対になっておらず、クリーニングのパージが痩せる

対象: `src/web/api/jobs/pasting/flow_calibration.py:215-238`

根拠:

- `FillSequence.to_gcode`（`src/pcbasm/pasting/fill_sequence.py:149-177`）は先頭で
    `retract_amount` を prime し、末尾で同量 retract する自己完結型。`run_flow_calibration`
    の最後の `deposit_at` を終えた時点で plunger は `retract_amount` 引き込まれている
- `_clean_and_purge` はその状態から `clean_nozzle` を呼ぶ。`clean_nozzle` は
    `applicator.load(clean.purge_ul)`（`nozzle_clean.py:232-233`）を押すだけなので、
    先端から実際に出るのは `purge_ul - retract_amount`。実 config
    （`config/machine.toml`: `retract_amount = 0.03` / `nozzle_clean.purge_ul = 0.1`）では
    0.1 uL のつもりで 0.07 uL しか出ない。ログは `clean_nozzle` が「パージ 0.100 uL」と
    出すので表示と実体がずれる
- 旧 `paste_solder` は `clean_nozzle` を FillSequence より前（プライムが抜けていない状態）で
    呼び、直後の `retract()` が唯一の「プライム → 引込み」遷移だった。新しい呼び出し位置では
    その前提が崩れている
- 同じ落とし穴は `src/web/api/jobs/pasting/paste_volume_calibration.py:587-590` に明記済み
    （「先頭で retract すると plunger が baseline より引き込まれた状態で全点が走る」）
- `src/web/api/routers/nozzle_cap.py:143-146` のテスト実行は「塗布ジョブが行うのと同じ手順」を
    謳って fresh applicator から `clean_nozzle` → `retract` する。テスト実行と本番ジョブで
    実際の吐出量が食い違う

派生（`purge_ul == 0` または `nozzle_clean is None` のとき）: 何も押していないのに `retract()`
だけが走り、plunger が `2 * retract_amount` 引き込まれる。`purge` も `None` ならその状態で
2 回目の測定 1 点目が走り、`retract_amount` ぶん（0.2 uL 指令に対し 0.03 uL = 15%）少なく
吐出する。詰まりを判定しようとしている当の測定にバイアスが乗る。

確信度: 高（機構）。0.07 uL で詰まりが抜けるかの実害判断は実機・ユーザー側。

## should-fix

### S1. 流量キャリブレーションが無効な基板ではクリーニングが一切行われなくなる

対象: `src/web/api/jobs/pasting/paste_solder.py:121-134`

`plan_flow_calibration`（`runtime.py:96-101`）は `not config.enabled or not points` で `None` を
返し、`points` は基板ごとの `model.flow_calibration_points`。測定点未設定の基板や
`calibration_file` 未設定のマシンでは `flow is None` になり `run_flow_calibration_with_cleaning`
を通らないので、クリーニングが 1 度も走らない。変更前は無条件に走っていたため機能が消失する。
`nozzle_cap.html` の文言にもこの条件は書かれていない。確信度 高（経路）／仕様の可否はユーザー確認。

### S2. `NO_DEPOSIT_DETECTED` が 2 つの意味を兼ねている

`estimator.py:136` の `detected_view_count == 0`（本当に写っていない）と `estimator.py:146-147`
の `mean <= 0.0`（検出はできたがモデル体積が非正）が同じ理由文字列。後者は塗布が写っている
ので、全点がそれに当たるとクリーニング → 異常終了へ誤って落ちる。確信度 中（経路は確実、
被覆域かつ信頼域の内側で `mean <= 0` が起きるかは校正モデル依存）。

### S3. `nozzle_clean` も `purge` も `None` のとき、何もせずに再測定して落ちる

`flow_calibration.py:198-211` / `230-249`。ログは「ノズルをクリーニングしてやり直します」、
失敗文は「クリーニングとパージの後も塗布を検出できません」だが、どちらも実施していない。
静定待ち込みで 1 サイクル無駄に回してから同じ結論に至る。確信度 高、影響小。

### S4. 2 回目の測定が同じ測定点へ重ね塗りする

`detected_view_count == 0` は「検出器が見つけられなかった」であって「出ていない」の証明では
なく、照明・コントラスト起因の検出失敗もここへ落ちる。1 回目が実際には吐出していた場合、
2 回目の pre 画像に 1 回目のドットが写り、直径 → 体積モデル（絶対直径前提）へ差分の増分が
渡って推定が過小になる。確信度 中。orchestrator メモの実機申し送り 3 は再パージ点の重ねだけを
挙げており、測定点の重ねは別問題。

### S5. `config_store.py:159` のコメントがドキュメント同期漏れ

`# [paste_dispenser.nozzle_clean] — 塗布開始時のノズル先端クリーニング（マシン座標）` のまま。
確信度 高。

### S6. `_clean_and_purge` の `applicator.retract()` に `ctx.progress` / `ctx.checkpoint` が無い

前後の 2 ステップには両方ある。中断の粒度が揃わない。確信度 高、影響小。

## nit

- `ctx.progress("パージ")` は `paste_solder` の「初回パージ」と別ラベルで、UI 上は別工程に見える
- `_clean_and_purge` の未記録ログが `nozzle_clean._NOT_RECORDED` と別文言。定数化の意図
    （WebUI と文言を揃える）から外れる
- テストが「クリーニング → パージ → 再測定」の順序を検証していない
    （`_approaches_to_clean` / `_purges` の回数だけで `moves` の並びは見ていない）
- 削除したコメント「ローディング直後の先端が最も汚れているので、掃除はその後に行う」の知見が
    失われた。対話的ローディング後は汚れた先端のまま 1 回目の測定に入る（仕様どおりだが記録価値あり）
- `resolve_nozzle_clean` の可動域エラーは今も塗布ジョブ全体を落とす。掃除が条件付きになったのに
    検証だけ無条件（計画で明記された判断）

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（3353 passed, 184 deselected）
- 成果物汚染（`</content>` 等）: なし
