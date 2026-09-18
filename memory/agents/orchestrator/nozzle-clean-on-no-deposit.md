# ノズルクリーニングを「塗布未検出時のみ」に変更

ブランチ: `feature/2026-09-18/clean-on-no-deposit`

## 確定仕様（ユーザー回答 + 2026-09-18 の訂正）

**訂正**: 検出は流量キャリブレーションではなく**初回パージそのもの**で行う。
パージ点を塗布前後で撮影し、ペーストが写らなければクリーニングする。

- 順序: パージ点を撮影 → パージ → 撮影 → 差分で検出 → 写らなければクリーニング
    → パージし直して再検出 → 写れば続行（流量キャリブレーションは従来どおり）、
    写らなければ**異常終了**
- 判定は `measure_dot` の `detected` だけを見る（体積は推定しない）。校正ファイルが
    要らないので、流量キャリブレーションを使わない機体でも確かめられる
- crop はパージ点中心の 4 mm 角（`PURGE_CROP_SIZE_MM`）。測定点のドットより広がるため
    流量キャリブレーションの crop（既定 2 mm）より広く取る
- 撮影・計測に失敗したときは判定を諦めて先へ進む（確かめられないことでは落とさない）
- ジョブ開始時の無条件クリーニングは**廃止**
- 判定条件は「先から推論」→ **全測定点が `no_deposit_detected`** を未検出とする
    （1 点でも検出できていれば吐出はできており、詰まりではない。被覆域外・撮影失敗は
    従来どおり「補正しないで続行」で、クリーニングも異常終了もしない）

## 公開インターフェース

- `pcbasm.pasting.paste_volume.estimator.NO_DEPOSIT_DETECTED`: 理由文字列の定数
- `pcbasm.pasting.paste_volume.runtime.no_deposit_detected(predictions) -> bool`
- `web.api.jobs.pasting.flow_calibration.FlowCalibrationRun(outcome, no_deposit)`
- `run_flow_calibration(...) -> FlowCalibrationRun`（戻り値型を変更）
- `run_flow_calibration_with_cleaning(ctx, session, correction, applicator, plan, *,
    nozzle_clean, purge) -> FlowCalibrationOutcome | None`（未検出が続けば ValueError）

## 判断と理由

- 再試行と異常終了の統括は web 層の `flow_calibration.py` に置く。`paste_solder` 本体は
    実機必須で単体検証できないが、`flow_calibration.py` は既存の合成ジョブ harness
    （FakeCamera / FakeKlipper + 実 PasteSession）で検証できる
- 未検出判定（全点が `no_deposit_detected` か）は装置に触れない純関数なので
    `paste_volume/runtime.py` に置く（webui-thin-wrapper: ドメインは pcbasm）
- クリーニング位置が未記録のときは、クリーニングだけを飛ばしてパージ + 再検出は行う。
    パージのやり直しにも詰まり解消の意味があり、分岐を増やさない
- `resolve_nozzle_clean` はジョブ開始時に呼んだまま残す。教示ミス（可動域外）を
    位置合わせ前に顕在化させる目的は変わらない

## 進捗

- [x] 段階 1 計画
- [x] 段階 2 テスト（red 確認済み）
- [x] 段階 3 実装（`make format && make type && make test-no-hardware` green）
- [x] 段階 5 ドキュメント（docstring / README / WebUI 文言）
- [ ] 段階 4 自己レビュー + code-reviewer
- [ ] PR

## 実装中の判断

- テスト容易性のため、再試行の統括は `paste_solder`（実機必須）ではなく
    `flow_calibration.py` に置いた。既存の合成ジョブ harness で FakeCamera に
    一様フレーム / 中心に暗い円のフレームを順に返させ、未検出 → クリーニング →
    パージ → 再検出 の全分岐を検証できる
- テストの「パージ回数」は、測定点とツールヘッドオフセットを含むマシン座標を
    `session.point_transform` から求め、その Y へ寄った連続移動のかたまりを 1 回と数える
    （座標即値をテストへ書かない）
- 既存 `run_flow_calibration` の戻り値を `FlowCalibrationRun`（outcome + no_deposit）へ
    変更。撮影失敗・校正未読は `no_deposit=False`（測れていないことは塗れていないことの
    証拠にならない）

## code-reviewer 指摘への対応（verdict: request-changes → 対応済み）

- **M1 プライム収支**（must-fix）: 掃除のパージは直前の `deposit_at` が残した引き込みを
    埋めるだけで目減りしていた。`PasteApplicator.prime()` を追加し、
    `prime → clean_nozzle → retract` で囲んだ。掃除しないときは引き込んだままにし、
    余計な `retract()` を送らない（送ると次の `deposit_at` が過剰プライムになる）
- **S1 流量キャリブレーション無効時はクリーニングが走らない**: ユーザーが Q3 で
    「廃止する（推奨）」を選んだ時点で許容済み。変更なし
- **S2 `no_deposit_detected` の二重用法**: モデルが非正体積を返す経路を
    `NON_POSITIVE_VOLUME` へ分離。円が写っている以上、詰まりの証拠にしない
- **S3 何も処置できないときの文言**: `_clean_and_purge` が実施した処置名を返し、
    失敗メッセージをそれに合わせた
- **S4 2 回目が同じ測定点へ重ね塗り**: 基板設定の点以外に塗る場所が無いので設計どおり。
    過小推定の可能性を docstring に明記し、実機確認の申し送りへ回した
- **S5** `config_store.py` のコメントを同期。**S6** retract を progress/checkpoint の内側へ
- nit「ローディング直後の先端が最も汚れている」という旧コメントの知見: クリーニングの
    契機が検出結果に変わったため、`paste_solder` からは落とした（開始時に掃除しない）

## ユーザーへの実機確認申し送り

1. 意図的に詰まった（またはペーストを抜いた）状態で塗布ジョブを走らせ、
    未検出 → クリーニング → パージ → 再測定 の順に動くこと
2. 2 回目も未検出ならジョブが FAILED で止まり、pad を塗り始めないこと
3. 再パージが初回パージと同じ点（既定は順路先頭 pad 中心）へ重ねて出る点の可否
4. 2 回目の測定は 1 回目と同じ測定点へ塗る。1 回目が実は出ていて検出だけ失敗した場合、
    2 回目の推定は増分ぶんで過小に出る（補正は 1/3〜3 倍で頭打ち）。ログの点ごと推定を確認
5. 掃除時のパージ量が設定どおり出ているか（prime を入れたので従来より retract_amount
    ぶん多く出る。`[paste_dispenser.nozzle_clean] purge_ul` の再調整が要るかもしれない）
