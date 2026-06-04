# P1 — 基盤の値オブジェクト（Path / Speed）

計画書: `/home/gop/.claude/plans/claude-pcbasm-hal-pcb-partitioned-fox.md` P1（加算的・keystone）。
`Path` / `Speed` は純粋な値オブジェクトのため **モック不要・実オブジェクトのみ**で構築。
3rd-party 表面・内部 private はテストしていない。

## 書いたテスト一覧

### tests/pcbasm/geometry/test_path.py::TestPath
- `test_length[empty/single/3-4-5/multi]` — 正常系/エッジ: ポリライン長 = 連続区間長の和。空[]→0.0、単一点→0.0、2点(0,0,0)-(3,4,0)→5.0、多区間(0,0,0)->(3,0,0)->(3,4,0)→7.0
- `test_transformed_shift` — 正常系: `Shift(1,2,3)` で各点が平行移動した新 Path
- `test_transformed_scale` — 正常系: `Scale(2,2,2)` で各点がスケール
- `test_transformed_compose` — 正常系: `Compose([Shift(1,2,3), Scale(2,2,2)])` を self[0]→self[1] 順で適用（(1,1,1)→(2,3,4)→(4,6,8)）
- `test_transformed_does_not_mutate_original` — 不変性: transformed 後も元 Path.points が不変
- `test_construction_from_list_and_generator_are_equal` — 正常系: list 由来と generator 由来の Path が等価（converter=tuple）
- `test_points_is_tuple` — 正常系: 公開 `points` が tuple
- `test_len` / `test_len_empty` — 正常系/エッジ: `len(path)`
- `test_iteration_yields_points_in_order` — 正常系: `iter(path)` が順序通り
- `test_getitem_first_and_last` — 正常系: `path[0]` / `path[-1]`

### tests/pcbasm/hal/test_speed.py::TestSpeed
- `test_absolute_ignores_max` — 正常系: `absolute(150).resolve(300) == 150`
- `test_absolute_ignores_different_max` — 正常系: 別 max でも 150（max 無視の確認）
- `test_rate_resolves_to_fraction_of_max[0.5/1.0/0.0]` — 正常系/境界: `rate(f).resolve(300)` = f*300（0.5→150, 1.0→300, 0.0→0）
- `test_rate_out_of_range_raises[-0.1/1.1]` — 異常系: 範囲外 fraction で `ValueError`

## 仕様根拠の対応表
- test_length → pinned API「polyline length = sum of consecutive segment lengths; 0.0 for 0 or 1 points」／計画書 `Path.length()`「Σ(points[i+1]-points[i]).norm(); <2点で0」
- test_transformed_* → pinned API「transformed(transform) returns a NEW Path with each point transformed」／計画書 `Path.transformed(t)`
- test_transformed_does_not_mutate_original → pinned API「original unchanged」「immutability of the value」（`@attrs.frozen`）
- test_construction_from_list_and_generator → pinned API「points is any Iterable[Point3d] (stored as a tuple)」／計画書「converter=tuple で iterable を受ける」
- test_len/iteration/getitem/points_is_tuple → pinned API「len(path), iter(path), path[i] (incl path[-1])」「path.points is the public tuple」
- test_absolute_* → pinned API「absolute returns mm_per_s (max ignored)」
- test_rate_resolves → pinned API「rate returns fraction*max_velocity」
- test_rate_out_of_range_raises → pinned API「rate(fraction) ... raises ValueError otherwise」／計画書「0<=fraction<=1 を検証」

## 期待される失敗（仕様 first の場合）
なし。P1 は **加算的**（failing-spec ではない）。並列 src 側が実装を landed 済みのため全テスト緑。

## 実装側に求める修正
なし。`pcbasm.geometry.Path` / `pcbasm.hal.Speed` は pinned API に完全準拠。

## tests/helpers.py への追加
なし。Path/Speed は純粋な値オブジェクトで fake 不要。3rd-party モックも内部 private モックも未使用。

## 検証結果
- make format: pass
- `uv run pytest -v -m "not hardware" tests/pcbasm/geometry/test_path.py tests/pcbasm/hal/test_speed.py`: 21 passed

## メモ（後続フェーズ向け）
- `@pytest.mark.api_contract` は未使用。`pyproject.toml` の markers に `api_contract` が未登録で、`--strict-markers` 下では失敗する。計画書 P1 も api_contract を要求していないため付与せず。公開 API 契約を別途ピンするなら先にマーカー登録が必要。
- `--doctest-modules` が有効だが test ファイルには doctest を書いていないため影響なし。
- P3 で `test_trajectory.py` 削除予定。`sort_by_nearest` 分は `test_routing.py` へ移すが、これは P1 スコープ外（本 P1 では既存テスト無改変）。
