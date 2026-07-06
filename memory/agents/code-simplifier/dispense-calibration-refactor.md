# 吐出量キャリブ リファクタ（5 コミット）の簡素化

対象: `feat(webui) 吐出量キャリブの実行中パラメータ編集UI` ほか直近 5 コミットで
追加/変更した新規コードのみ（既存周辺は外科的に不可侵）。

## 簡素化した内部実装

### `src/webui/jobs/catalog.py` `_coerce_param`
removal_z_offset の負値ガードを二重 if → 単一 if に統合。

```python
# before
if spec.name == "removal_z_offset" and isinstance(coerced, (int, float)):
    if coerced < 0:
        raise ValueError(...)
# after
if (
    spec.name == "removal_z_offset"
    and isinstance(coerced, (int, float))
    and coerced < 0
):
    raise ValueError(...)
```

判断理由: 内側 if の `coerced < 0` は外側と独立した第 3 の AND 条件にすぎず、
分割に意味はない（型ガードと値ガードを段で分ける理由はコメント上も無い）。
`and` 連鎖でも短絡評価・pyright の型ナローイング（`isinstance` で int|float に
絞ってから `< 0`）は同一。ネストが 1 段減り読みやすくなる明確な簡素化。
指示文の「2 段化」は対象記述であって不変条件ではない（不変条件節に無し）。

## 公開 IF 維持の確認
- シグネチャ・戻り値・例外契約は不変（`_coerce_param` は内部関数、挙動同一）。
- `validate_runtime_params` / `validate_params` 両経路の負値拒否は維持。
- test_catalog.py（負値→ValueError / 0・正値→受理）、test_jobs.py（負値→400）が
  全て通過。

## 簡素化できなかった/しなかった部分・理由
- `validate_runtime_params` の `editable`(dict) と `fixed`(set) の二重構築:
  「固定キー（変更不可）」と「未知キー」を別メッセージで弾くために両方必要。統合不可。
- `pasting.py` の `_CalibrationCancelled` / `_prompt_confirm` / `_prompt_mass` /
  `_removal_z`: 中止フローの例外化・退避 Z ヘルパー化は既に重複除去済みで過剰分割なし。
  これ以上触ると「ただ違うコード」になり振る舞いリスクが上回る。
- `manager.py` ライブストア（`_params_lock` + `_params`）と `context.py` の
  `live_params()` 委譲: out-of-band ライブ適用という要件そのもの。簡素化余地なし。
- `dispense_runtime_params.js`: 既に薄ラッパー（空欄スキップ + Number.isFinite の
  パース可否のみ、ドメイン検証はサーバ）。短縮の必要なし。
- `JobParamsUpdateRequest` / `put_current_params` / `JobDefinition.runtime_params`:
  最小実装。変更不要。

結論: 明確な簡素化は catalog.py の 1 箇所のみ。他は「これ以上簡素化不要」。

## 検証結果
- make format: pass
- make type: pass（pyright 0 errors）
- make test-no-hardware: pass（1401 passed, 70 deselected）

git commit はしていない（メインが検証後にコミット）。configs/ は未変更。
