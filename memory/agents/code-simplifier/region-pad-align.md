# region-pad-align

code-reviewer（memory/agents/code-reviewer/region-pad-align.md）の should-fix 1 件・nit 1 件を修正。

## 修正1: `_validate_region_fits_frame` の回転矩形bbox式（should-fix）

`src/pcbasm/posctrl/alignment.py:129-163`

- 誤: `required_width = region_size[0] * rho`, `required_height = region_size[1] * rho`
  （rho = |cosθ|+|sinθ| を両軸に一律適用。w=h の正方形でのみ数学的に一致し、非正方形では
  幅を過大要求・高さを過小検証する数値的に不正な式だった）
- 正: `required_width = w*|cosθ| + h*|sinθ|`, `required_height = w*|sinθ| + h*|cosθ|`
  （w, h = region_size。回転矩形の軸並行外接矩形（AABB）の標準公式）
- 実装: `rho` 変数を廃止し `cos_theta = abs(cos θ)` / `sin_theta = abs(sin θ)` を個別に保持、
  `width, height = region_size` で展開してから両辺を個別計算する形にした（式は2行→4行だが
  各行が対応する幾何式そのままで可読性は同等以上）
- docstring も「領域サイズ×ρ」の記述を正しい2式の記述に更新
- エラーメッセージの構造（`region_size[0]:.2f x region_size[1]:.2f` 等のフォーマット・
  文言）は無変更

## 修正2: `_DEFAULT_MIN_ROI_MM` の値（nit 採用）

`src/pcbasm/posctrl/render.py:31`

- 3.0 → 1.0 に変更
- 根拠: `git show main:configs/kurousagi/machine.toml` / `test-fixture/machine.toml` を
  確認したところ、いずれも `pad_align.min_roi = 1.0`。旧実装（`git show main:...render.py`）は
  この値を `roi_of(..., min_size_mm=pad_align.min_roi)` と `_centered_roi(..., pad_align.min_roi)`
  にそのまま渡していた。`min_roi` 設定削除に伴う定数化で、クラス既定値（config.py の
  `PadAlign.min_roi: float = 3.0`）ではなく実機 configs の運用値 1.0 を保存するのが
  挙動維持として正しい
- コメントに「旧configsのpad_align.min_roi既定運用値(kurousagi/test-fixtureとも1.0)を保存」
  を追記

## 公開IF維持の確認

- `_validate_region_fits_frame` / `_DEFAULT_MIN_ROI_MM` はいずれもモジュール内 private。
  公開IF（`PadAlignmentSession.__init__` の ValueError 契約、`PadResultRenderer.__init__`
  シグネチャ）は無変更
- `PadAlignmentSession.__init__` の ValueError 契約テスト
  （`test_init_raises_value_error_when_region_size_does_not_fit_field_of_view`、正方形
  region_size を使用）が引き続き pass することを確認済み

## 検証結果

- `make format`: pass（自動整形による差分なし）
- `uv run pyright src`: 0 errors, 0 warnings
- `uv run pytest tests/pcbasm/posctrl/test_alignment.py tests/pcbasm/posctrl/test_render.py`:
  22 passed
- `tail` によるファイル末尾確認・`grep '</content>'`: 混入なし（両ファイルとも）
- コミットはしていない（指示どおり）
