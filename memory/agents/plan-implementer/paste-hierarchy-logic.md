# paste-hierarchy-logic (Phase 1: pcbasm 純ロジック)

新規 `src/pcbasm/pcb/grouping.py` と `src/pcbasm/pasting/settings.py`。HAL 非依存。
spec-test-author と並列。IF はシグネチャレベルで承認済み計画書どおり実装、逸脱なし。

## 計画外の判断ログ（IF 自体は不変、内部実装の確定事項）

1. **PadShapeKey.label の量子化単位**: `PadShapeKey` は量子化済み int (`short_q`/`long_q`/`area_q`) しか保持しないため、`.label` は `DEFAULT_SHAPE_QUANTUM = 0.01` を仮定して mm を復元する近似表示。`of()` に `quantum != 0.01` を渡すと label の数値が実寸からずれる（キー一致性には影響しない）。計画は shape_quantum 既定 0.01 のみ使用するため実害なし。label はあくまで人間向け。
2. **label フォーマット**: 非カスタム = `f"{short:.2f}x{long:.2f}mm"`（例 `"0.65x1.06mm"`、`x` は ASCII。計画文中の `×` ではなく `x`）。カスタム形状 = `f"custom {short:.2f}x{long:.2f}mm ({area:.2f}mm2)"`。`mm²` ではなく `mm2`（codespell/ASCII 安全）。
3. **MRR 辺長抽出**: `polygon.minimum_rotated_rectangle` の exterior 連続2頂点間距離から隣接2辺を取り min/max で短辺/長辺に正規化（fill_path.py の mrr 利用と同流儀）。縮退時 (0.0, 0.0)。
4. **L0 ラベル** = `"全部品"`、L1=package、L2=designator、L3=shape_label、L4=`f"{designator}.{pad_number}"`。
5. **L1 出現順**: `ordered_pads`（= pads 入力順、対応 Component あり）での package 初出順。L2/L3/L4 も同様に出現順保持。
6. **PadHierarchy の private 属性**: `_keys_by_pad: dict[(designator,pad_number), tuple[HierKey,...]]`（5キー）を保持。`attrs.field(alias="keys_by_pad", eq=False)` を付与。理由: attrs は `_` prefix の init 名から `_` を剥がすが pyright (attrs stub) がその alias を推論せず reportCallIssue になるため、明示 alias で runtime と型を両立。`eq=False` は等価判定を root のみに限定（派生キャッシュなので）。
7. **追加の public メソッド** `PadHierarchy.all_keys() -> set[HierKey]`: `find_orphans` が「現 hierarchy の全ノードキー集合」を必要とするため公開。計画 IF には無いが追加 public（spec-test-author が依存しても問題ないシグネチャ）。**IF 追加通知**（下記参照）。
8. **`node_keys_for_pad`**: pad が階層に無い場合 `KeyError`（docstring に明記）。計画通り「対応 Component が無い pad は除外」なので、除外 pad を渡すと KeyError。
9. **settings JSON**: 計画どおり `version`/`source_pcb` を付けない（webui 責務）。`override` は非 None 項目のみ、`enabled` は `bool | null` を常に出力。`levels` は list（tuple キー JSON 不可のため）。`settings_from_dict` は list の key を tuple 化、欠落 override 項目を None 復元、`levels`/`base`/`base_enabled` 欠落にも頑健（`.get` で空デフォルト）。
10. **module-level エイリアス/定数の docstring 化を回避**: `HierKey`/`DEFAULT_SHAPE_QUANTUM`/`PASTE_OVERRIDE_FIELDS`/`EnableState` の直後に bare string literal を置くと docformatter が「Multiple module docstrings」で落ちるため、すべて行コメントに変更。

## 他 implementer / spec-test-author への IF 通知

- **IF 追加（非破壊）**: `PadHierarchy.all_keys() -> set[HierKey]` を public 追加。計画 IF の `iter_pads`/`node_keys_for_pad` は不変。
- **IF 追加（非破壊）**: `pcbasm.pcb.grouping.DEFAULT_SHAPE_QUANTUM = 0.01` を module 定数として公開（`pcbasm.pcb` の `__all__` には未追加）。
- 計画 IF のシグネチャ（PadShapeKey/PadHierarchyNode/PadHierarchy/build_pad_hierarchy、PasteOverride/LevelSetting/ResolvedPaste/PasteSettingsModel、base_override_from_config/resolve_pad_settings/find_orphans/settings_to_dict/settings_from_dict、PASTE_OVERRIDE_FIELDS/EnableState）は**完全一致**。差異なし。
- **label の具体文字列**（`"0.65x1.06mm"` 等の桁・区切り）にテストを固定する場合は上記 2 のフォーマットに合わせること（`x` 区切り・小数2桁・カスタムは `custom ... mm2`）。

## export

- `pcbasm.pcb.__init__`: `HierKey, PadHierarchy, PadHierarchyNode, PadShapeKey, build_pad_hierarchy` を追加。
- `pcbasm.pasting.__init__`: `PASTE_OVERRIDE_FIELDS, EnableState, LevelSetting, PasteOverride, PasteSettingsModel, ResolvedPaste, base_override_from_config, find_orphans, resolve_pad_settings, settings_from_dict, settings_to_dict` を追加。

## 既知の制約・残課題（次フェーズ）

- Phase 2 (applicator per-pad override)・Phase 3 (webui 永続化/API) はこの純ロジックに依存。
- スモーク: 実 PCB `data/testing/led_blinker`（7 部品 / 18 pad）で L3 ラベルが SOT-23-6=`0.65x1.06mm` / 0402=`0.54x0.64mm` / 0603=`0.80x0.95mm` / LED=`0.88x0.95mm` と妥当に分離。回転不変・override マージ・enabled 最具体勝ち・orphans・JSON round-trip を Python スモークで確認済み。

## 検証結果

- make format: pass
- make type: pass (0 errors)
- make test: 未実行（tests/ は spec-test-author 担当・合流時に親が collection 確認。本 agent は format/type までの取り決め）
- 追加スモーク（python -c）: ALL PASSED
