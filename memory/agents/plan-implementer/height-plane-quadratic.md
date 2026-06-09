# height-plane-quadratic (plan-implementer)

承認済み計画: `.claude/plans/height-plane-0-1mm-wild-pascal.md`
担当範囲: `src/` と `configs/` のみ（`tests/` は spec-test-author が並行担当、未編集）。

## 実装した変更

1. `src/pcbasm/geometry/transform.py` `HeightPlane`
   - private 係数を 6 個に拡張（`_c, _a, _b, _d, _e, _f`、`init=False, eq=False`）。
   - `__attrs_post_init__`: 点数<6 で ValueError（"6点以上"）。design 行列 `[1, x, y, x², y², xy]`、`matrix_rank(tol=1e-9)<6` で退化 ValueError（"退化"）。`lstsq(rcond=None)` で 6 係数取得。
   - `apply(Point3d)`: `z_offset = c + a*x + b*y + d*x² + e*y² + f*x*y`。Point2d 素通し。overload 2 本維持。
   - `inverse`: 既存のまま（各点 z 反転で再構築）。docstring を 2 次曲面用に更新。
2. `src/pcbasm/config.py` `Probe`
   - `min_samples` デフォルト 3→6、ガード `<3`→`<6`、メッセージ "6以上"。max_samples デフォルト 9 据え置き。
3. `configs/kurousagi/machine.toml` `[probe]`
   - `min_samples = 6` 追加、`max_samples = 12` 追加（元はデフォルト 9 で未記載）。他セクション不変。
4. `src/pcbasm/pasting/height.py`
   - クラス/`measure` docstring「平面フィット」→「2次曲面フィット」。ロジック変更なし。
5. `src/scripts/pasting/height_plane.py` `_visualize`
   - グリッド/extent を probe bbox → `pcb.outline.polygon.bounds` に変更。scatter(xs,ys,zs)/背景/colorbar は維持。argparse description「平面フィット」→「2次曲面フィット」。

## 判断ログ / 計画外事項

- 計数フィールド名・係数順序は計画通り（`_c` 先頭、lstsq 係数も `[1,x,y,x²,y²,xy]` 順に対応させた）。numpy 1 本保持は採らず既存の名前付きスタイルを維持（計画の優先指示通り）。
- 計画逸脱なし。公開 IF（`HeightPlane(points)` / `apply` / `inverse`）はシグネチャ不変。
- IF 変更通知: なし。

## 検証

- `make format` パス（comment 再フロー以外の変更なし）。
- `make type`（pyright）: 0 errors。
- スモーク: 既知 2 次曲面の係数復元・Point2d 素通し・inverse 符号反転・6 点未満/退化 ValueError・kurousagi config 構造化（min=6,max=12）すべて確認。
- `make test-no-hardware` は spec-test-author のテスト未着につき今は対象外。import 起因の破壊は無し（transform/config/height モジュール import OK）。
