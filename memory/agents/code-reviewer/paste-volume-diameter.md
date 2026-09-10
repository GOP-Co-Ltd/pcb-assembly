# code-reviewer: 点塗布の直径ベース塗布量推定（MR1: パージ廃止）

## MR1 verdict: request-changes → 全件対応して解消

### must-fix

| # | 指摘 | 裁定と対応 |
|---|---|---|
| M1 | ローディング待機中、ノズルが銅板の真上・AirPump ON のまま無期限ブロック。押すと板へ落ち、配分式が全 sample で系統的にずれる | **受理**。`_move_clear_of_plate` を追加し、applicator を開く前に board (0, -15mm) へ退避。可動域外なら移動せず jog を促すログに留める（装置を止めるほどではない） |
| M2 | 回転ローディングの ParamSpec 4 つが UI に届かず完全に死んでいる（`loading_controls.html` の回転セクションが `loading_rotation_defaults` で gate されている）。実レンダリングで実証 | **受理・選択肢1を採用**（回転 UI を出す）。ユーザーの「インタラクティブローディング」は既存 `loading` ジョブと同等の操作性を指すと解釈。`_paste_dataset_context` が `loading_` prefix を落として既定値を渡す。**再発防止に UI 実描画のテストを追加** |
| M3 | `skip_if_no_real_sessions` が「現版が1つでもあるか」なので、v3 を1本収集した瞬間にゲートが開き旧版 11 本で `from_roots` が失敗する | **受理**。`current_schema_sessions()` が現版 session の**パス列**を返し、テストはそれを `from_roots` へ渡す。旧版混在（実測 v1×5/v2×6）でも壊れない。tmp で実証確認済み |

### should-fix

| # | 指摘 | 対応 |
|---|---|---|
| S1 | 収集ジョブ docstring がパージ時代の前提のまま（「手動ローディングを挟まない」「この前提（手動プライム）」） | 受理・修正 |
| S2 | 要件書 L327 に同じ取り残し | 受理・修正 |
| S3 | テスト docstring が v2 のまま 5 箇所 | 受理・修正 |
| S4 | 収集ジョブのローディング組み込みにテストが無い | **部分対応**。フルランは実 Klipper が要るため、`loading_param`／`accepts_commands`／回転 ParamSpec の構造ピンと、M2 の UI 実描画テストで再発防止を敷く。実行経路そのものの確認はユーザーの実機検証に委ねる（計画どおり） |
| S5 | `capacity == len(grid)` が恒真で UI に同じ数が 2 回出る | 受理。表示を「格子 N セル（すべて計測可能点）」の 1 つへ集約。フィールドは `plan` 側のエラーメッセージで使うので残す |

### nit

| # | 指摘 | 対応 |
|---|---|---|
| N1 | `PASTING_LOADING_FEATURES` が stale（未参照の死んだ定数） | 受理・削除。docstring の「4 feature」も修正 |
| N2 | ピン fixture のキー順が DTO 出力順と食い違う | 受理。`to_dict()` の出力で書き直し、要件書の JSON 例も `total` → `loading` へ |
| N3 | `validate_dispense_reach` の空リスト初期化が冗長 | 受理。`for cell in plan.cells` の直接ループへ |
| N4 | `LoadingTotals.amount_ul/.rotations` と `PasteDatasetLoading.total_ul/.total_rotations` の名前揺れ | **却下**。`LoadingTotals` は web 層の既存型で、改名はこの MR の要求外。変換は 1 箇所のみで実害なし |

### レビューで確認され問題なしとされた点

要求 1（除外領域の廃止）/ 3（配分式から purge が消える）/ 4（v3 のみ・v2 を明示的に拒否）/
5（`src/ml/` 不変更）、スコープ外（`paste_solder` の初回パージ・testboard の PURGE pad）の温存、
seed 期待値の再確定が緩めでないこと、規約準拠。
