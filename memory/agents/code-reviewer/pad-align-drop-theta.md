# pad-align-drop-theta レビュー

計画書: `/home/gop/.claude/plans/claude-maximize-parallels-majestic-pike.md`
対象: working tree 全体（`fix/20260729/pad-align-drop-theta`、未コミット）

## verdict: approve（should-fix 1 件は tolerance の再判断が要るため、着地前に裁定を推奨）

θ 撤去そのものは端から端まで正しい。`correction.py` 無改造で `M` が純並進になることを
数値検算で確認（det<0・shear・affine を含む R すべてで lever-arm 偏差 ≤ 3e-14）。
削除範囲も計画どおりで、温存対象（`crop_size` / `_template_rect` / `roi=None`）と
`test_correction.py` の回転共役ピンは無傷。

## must-fix

なし。

## should-fix

### S1. 収束閾値は 1px ではなく √2 px。警告ガードが実機設定で沈黙する（確信度: 数式=高 / 実害頻度=中）

`src/pcbasm/posctrl/alignment.py:126`

```python
if pad_align.tolerance < 1.0 / self._pixel_per_mm:
```

収束判定は `position.py:90` の `offset.norm < tolerance`、`offset = R(d)` で R は
`OffsetTransformMeasurer.measure()` が返す純 `Rotation`（ノルム保存）。よって
`offset.norm == |d|`、d は整数 px。取り得る非ゼロノルムは 1px, √2px, 2px… なので
**斜め 1px（±1, ±1）の残差が収束するには tolerance > √2/ppm が必要**。

- 実機 ppm=30.225: 1px=0.03309 / √2px=0.04679、tolerance=**0.04** → ちょうど隙間に入る
- `data/testing/config/machine.toml` ppm=40: 1px=0.025 / √2px=0.03536、tolerance=0.03 → 同じ隙間

つまり §4 が「非収束の原因が沈黙するのを防ぐ」ために足した警告が、§4 が新たに作った
設定でちょうど出ない。実害は「(1,1) 量子化に当たった部品で 1 反復余分（move+settle 0.5s）」が
主で、10 反復使い切る確率は低いが、`paste_solder` は `max_failures=0` なので 1 部品の
非収束が即中止になる。

分離できる 2 点:

1. ガード閾値 `1.0 / ppm` → `math.sqrt(2) / ppm`（コード側、判断不要）
2. `tolerance` 0.04 → 0.05（`PadAlign` の既定値と一致、√2px=0.0468 を超える）。
   実機値なのでユーザー判断。テンプレート `data/config-templates/kurousagi.paste/machine.toml:43`
   のコメント「1px（1/pixel_per_mm）より大きくする」も同じ理由で √2 倍へ直す

### S2. det<0（鏡映）の offset_transform がテストの parametrize から落ちた（確信度: 高 / 深刻度: 低）

`tests/pcbasm/posctrl/test_pad.py:196-199` の parametrize は
`Rotation(0) / Rotation(30) / Compose([Rotation(90), Shift])` の 3 本で、すべて det>0。
削除された `test_rotation_flips_sign_under_mirror_transform` が唯一の鏡映ケースだったため、
今回の変更で det<0 のカバレッジがゼロになった。`Scale.flip(y=True)` を 1 要素足すだけで戻る
（純並進が成り立つことは検算済み。`Scale` の import 復活が要る）。

### S3. `_dummy_match()` は `_dummy_match_with_offset(Point2d(0,0))` と同一（確信度: 高 / 深刻度: 低）

`tests/pcbasm/posctrl/test_pad.py:129,137`。パラメータ付きヘルパー 1 本に寄せられる。
`test_alignment.py:107` の `_dummy_match` は別ファイルなのでそのままでよい。

## nit

- `tests/pcbasm/posctrl/test_alignment.py:334` `test_align_result_transform_has_no_lever_arm`
  は θ 復活を確実には捕まえない。合成画像で θ スイープが 0.0 を返せば共役は恒等になり
  パスし得る。実効的なピンは `test_pad.py` の unit 側（`EdgeMatch` に rotation/center_mm が
  戻れば TypeError で落ちる）。統合スモークとしては有効なので削除は不要。
- レバー腕ピンが 3 本（unit / result.translation / 統合）。層が違うので重複とまでは言わないが、
  `test_result_translation_applies_to_every_pad_of_the_component` は
  `test_displacement_is_independent_of_lever_arm` のほぼ部分集合。
- `test_sub_pixel_tolerance_logs_warning` が警告文の "1px" というリテラルに依存。
  S1 で文言を触ると連鎖して落ちる。
- 新規 docstring が参照する「計画書 pad-align-drop-theta.md」はリポジトリ内に存在しない
  （`~/.claude/plans/` 配下）。`memory/agents/*/pad-align-drop-theta.md` は実装/テスト作者ノート。
- `_session(..., machine=...)`（test_alignment.py:298）の
  `machine if machine is not None else _machine_config()` は `machine or ...` で足りる。
- deploy 済み machine.toml に残る `theta_range` は cattrs（`forbid_extra_keys=False`）が
  黙って無視し、config_store の PUT でも tomlkit が原文保持するため永久に残る。
  実装者ノートのとおり移行は不要だが、他機の toml に dead key が残ることは記録しておく。

## 依頼された検算ポイントへの回答

1. **`correction.py` 未変更 / `M` の純並進性** — OK。`M = ψ_obs ∘ Shift(d) ∘ ψ_anchor⁻¹`、
   ψ_a(o) = a − R(o) なので線形部は (−L)·I·(−L⁻¹) = I。R が鏡映・shear・affine でも成立。
   数値確認: `Rotation(0/30)`, `Scale.flip(y)`, `Rotation(37)+flip(x)`, det<0 の `Matrix2d`,
   `Compose([Rotation(90), Shift])`, `Scale(2,3)` の全ケースで lever-arm 偏差 ≤ 2.9e-14。
   M(p) = p + (observed_at − anchor − R(d))。
2. **`match_rigid` → `match` の並進算出** — 不変。`git show HEAD:...copper.py` と突き合わせて
   旧 `match` の本体（`origin + min_loc`、`TM_CCORR` + `minMaxLoc` の最小値、
   `min_val / count_nonzero(template)`）が 1 行も変わっていないことを確認。`_prepare_match` も無改造。
   `roi` は旧 `match_rigid` と同じく `_template_rect` へのフォールバック付きで渡るだけ。
   スコア正規化は旧 `_sweep_thetas` の `min_val/edge_count` と θ=0 で厳密一致
   （identity warpAffine + `>0` 再二値化は template と同じ非ゼロ数）。`mean_distance_px` の
   意味は不変（値は θ 最良を取らなくなる分わずかに大きく出る）。ROI がフレーム端でクランプ
   されるケースも `origin = (sx0−x0, sy0−y0)` が吸収する構造のままで、変更なし。
3. **tolerance 警告** — `logger.warning(` を lazy %-format で 1 回、モジュール
   logger 使用、pre-commit の `use logger.warning(` フック通過。過剰検証ではない（config 境界）。
   ただし閾値が誤り → S1。
4. **削除漏れ / 削除しすぎ** — `git grep -ni theta` はリポジトリ全体で src=0 件、
   残るのは新規 docstring と `test_correction.py` のローカル変数のみ。`match_rigid` /
   `RigidEdgeMatch` の参照も 0（memory/ の過去ノートを除く）。温存対象
   （`crop_size` / `_template_rect` / `roi=None`）は無傷で、`roi=None` 経路は
   `TestCopperEdgeMatcher` が今も通している。orphan import（`Compose` / `Rotation` /
   `Scale` / `_rotated_vertices`）は過不足なく除去。
5. **`test_correction.py`** — 無変更（git status に出ない）。
   `test_camera_rotation_conjugates_to_same_machine_rotation` /
   `test_mirror_offset_transform_flips_machine_rotation_sign` とも健在。
6. **テスト品質** — 3rd-party（cv2 / picamera2 / Klipper RPC / time.sleep）のモックはゼロ。
   カメラは `FakeCamera`（自前 HAL Camera）、klipper/stage は既存 `mocker.Mock` イディオム、
   エッジ検出は実 `CopperEdgeDetector`。private への直接アクセスなし。
   レバー腕ピンの有効性は nit 1 件目のとおり（unit 側は有効、統合側は保証なし）。
7. **設定配線** — `config.py` / `webui/config_store.py` / `board_ops.py` /
   machine.toml 3 本すべて追従済み。WebUI 側は `MACHINE_FIELDS` 由来の動的描画で、
   テンプレート・JS に `theta` のハードコードなし（`git grep 回転探索` = 0 件）。
   `test_settings_api.py:39` / `test_config_store.py:53` は `MACHINE_FIELDS` から
   集合を導出しているため自動追従。e2e も影響なし（実行して確認）。

## 検証結果

- `make format`: pass（全フック Passed、ファイル変更なし）
- `make type`: pass（0 errors, 0 warnings）
- `make test-no-hardware`: pass（1610 passed / 87 deselected）
- `make test-e2e`: pass（51 passed / 1646 deselected）
- `grep -rn '</content>' src tests config data`: 0 件
- 実機テスト（`make test` / `@mark_hardware`）: 未実行（方針どおり）
