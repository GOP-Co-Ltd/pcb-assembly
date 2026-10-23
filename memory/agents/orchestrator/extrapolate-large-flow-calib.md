# 運転時キャリブレーションで被覆域の上限より大きい直径を外挿する

branch: `feature/2026-10-23/extrapolate-large-flow-calib`（solo-dev-cycle、レビューは code-reviewer へ単発委譲）

## 要求

運転時の動的流量キャリブレーションは、校正の被覆域 [min, max] の外の直径を推定に使っていない。
小径側はばらつくので今のまま拒否する。大径側は曲線がよく近似できているので、max を超えても推定値を使う。

## 計画

- 外挿を許すのは `reliable_range_only=True`（運転時）だけ。校正の生成・検証は被覆域のまま
- `CubicVolumeModel.covers_reliably` の上限を外す。ただし [max, d] で導関数 ≥ 0 のときに限る
    （3 項モデルは a<0 を取り得るので、被覆域の外で折れ返る）
- 不採用理由: 下限未満と折れ返り後はどちらも `diameter_out_of_calibrated_range`、
    [min, reliable_min) は `diameter_below_reliable_range`。新しい理由文字列は足さない
- 外挿の幅に上限は付けない。外れたときは既存の 1/3〜3 倍の頭打ちに任せる

## 却下した案

- 外挿の幅に固定の上限（例: max の 1.5 倍）を付ける: 要求にない。根拠になる実測もない
- 外挿した点に別の理由・フラグを付ける: 補正計算は採用か不採用かしか見ないので使い道がない

## 自己レビュー

- 導関数の最小値を求める式が `is_monotonic_in_range` と重複していたので、private の `_slope_min` に抽出した

## code-reviewer（request-changes）と裁定

- must-fix: 外挿に上限がないと、スケール不一致や点の結合で大きく測った点を採用してしまう。
    頭打ちは補正を止めず、1/3 の係数で塗る（実素材で 8.7 倍の過大推定を再現）。
    → ユーザー裁定で「被覆幅に比例した上限」を採用。`EXTRAPOLATION_MARGIN = 0.5`、
    `extrapolation_diameter_max_mm = max + 0.5·(max − min)`。計画の「外挿幅に上限を付けない」は撤回
- should-fix: 上限側の不採用理由が下限側と同じ文字列だった。
    → ユーザー裁定で `diameter_above_extrapolation_range` を新設（外挿の上限超え・減少に転じた先）
- should-fix: 引数名 `reliable_range_only` が上限を広げる挙動を表していない。
    → 改名しない。公開 IF の変更になり、要求の範囲を超える。docstring で説明する
- nit: テストクラスの docstring、`flow_calibration.py` のモジュール docstring、重複していた `covers` テストを直した
- nit（対応しない）: 採用した点が外挿だったかをログに出す、外挿した点の std。補正の計算に影響しない
