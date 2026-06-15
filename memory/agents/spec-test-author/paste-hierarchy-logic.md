# Phase 1 — pcbasm 純ロジック（階層 group-by + override 解決）テスト

対象計画: `/home/gop/.claude/plans/claude-webui-1-pad-extract-eager-pine.md` Phase 1
公開 IF: ユーザー指示で共有された厳守シグネチャ（plan-implementer と共有）。
区分: すべて unit。モック不使用。実 Pad/Component/shapely.Polygon と実 PasteDispenser
config（`data/testing/machine.toml`）を直接構築。PcbFile/pcbnew は使わない。

注: mirror パスは `tests/pcbasm/`（package 名は `pcbasm`。skill の例の `pcb_assembly`
はジェネリック名）。

## 書いたテストファイル

- `tests/pcbasm/pcb/test_grouping.py`
- `tests/pcbasm/pasting/test_settings.py`

helpers.py への追加: なし（Pad/Component を直接組むため不要。3rd-party モックも未使用）。

## test_grouping.py — 観点と仕様根拠

### TestPadShapeKey（L3 形状分類の契約）
- test_same_size_rectangles_at_different_positions_share_key — 正常系。0402 の 2 端子
  のような同サイズ矩形を別位置 → 同キー。計画「L3 = pad 形状・サイズで分類」。
- test_rotated_same_shape_pad_shares_key — エッジ。同矩形を 90 度回転配置 → 同キー
  ＋同 label。計画「回転配置された同型 pad は同 L3 グループ（回転不変）」。
- test_thermal_pad_and_signal_pad_have_distinct_keys — 正常系。大面積 vs 小矩形 →
  別キー。計画「熱パッドと信号 pad を区別」。
- test_custom_shape_flag_distinguishes_key — エッジ。同外形でも is_custom_shape 差で
  別キー。IF の PadShapeKey.is_custom_shape 仕様。
- test_label_is_human_readable_string — 正常系。label が非空文字列。IF の .label。
- test_quantum_collapses_near_identical_shapes — エッジ。微小寸法差は quantum 内で同一
  視。IF の `quantum` 既定 0.01。

### TestBuildPadHierarchyKeys（L0–L4 キー規約）
- test_root_is_l0 — root.key=("L0",)、level=0。計画「階層キー規約 L0」。
- test_l1_key_is_package — ("L1","0402")。「L1=("L1",package)」。
- test_l2_key_is_designator — ("L2","R1") level=2。「L2=("L2",designator)」。
- test_l3_key_is_designator_and_shape_label — ("L3","R1",shape_label) level=3。
  「L3=("L3",designator,shape_label)」、shape_label=PadShapeKey.of(pad).label。
- test_l4_key_is_designator_and_pad_number — ("L4","R1","1"/"2")。
  「L4=("L4",designator,pad_number)」。
- test_node_keys_for_pad_returns_five_levels — node_keys_for_pad が [L0,L1,L2,L3,L4]
  の 5 キーを順に返す。IF の node_keys_for_pad 仕様。

### TestBuildPadHierarchyShapeGrouping
- test_rotated_same_shape_pads_share_l3 — 回転配置 pad が同 L3 ノード配下。
- test_thermal_and_signal_pads_in_separate_l3 — 同 designator 内で熱/信号が別 L3。

### TestBuildPadHierarchyExclusion（対応 Component 無し pad の除外）
- test_pad_without_matching_component_is_excluded — iter_pads に出ない。
  計画「対応 Component が無い pad は階層から除外（group_pads_by_component と同方針）」。
- test_excluded_pad_does_not_create_l2_node — 除外 pad は L2 ノードも作らない。

### TestPadHierarchyNodePads（node.pads が配下葉 pad を全包含）
- test_root_holds_all_pads / test_l2_node_holds_all_pads_of_designator /
  test_l1_node_groups_all_same_package_pads / test_l4_node_holds_single_pad。
  IF「PadHierarchyNode.pads = このノード配下の全 pad（葉まで）」。

### TestIterPads
- test_iter_pads_yields_all_included_pads — 除外後の全 pad を列挙。IF の iter_pads。

## test_settings.py — 観点と仕様根拠

### TestBaseOverrideFromConfig
- test_copies_all_seven_override_fields — 実 config（machine.toml）の 7 項目を写す。
  IF「base_override_from_config(config)。7 項目を写す」。
- test_all_seven_fields_are_non_none — base は全 7 項目非 None（L0 確定値）。

### TestResolvePadSettingsKeys
- test_keys_are_designator_pad_number_pairs — 戻りキー=(designator,pad_number)。
- test_all_values_resolve_to_base_when_no_levels — levels 無しなら全 pad が base 値、
  enabled=base_enabled。

### TestOverrideMerge（非 None 項目のみ上書き、未指定は継承）
- test_level_overrides_single_field_keeps_others_inherited — 単項目上書き＋他継承、
  別部品は無影響。
- test_more_specific_level_wins_over_less_specific — L4 が L2 を上書き、未指定は L2/base
  から継承。同部品別 pad は L2 が効く。計画「最具体レベルが勝つ」。
- test_l1_package_override_applies_to_all_same_package — L1 override が同 package 全 pad。

### TestEnabledResolution（enabled は最具体が勝つ）
- test_l2_disable_disables_all_pads_of_component — L2=False で配下無効、他部品無関係。
- test_l4_enable_revives_pad_disabled_at_l2 — L2=False でも L4=True で復活。計画の
  キモ「L2 で enabled=False でも L4 で enabled=True なら最終 True」。
- test_base_enabled_false_defaults_all_pads_disabled — base_enabled=False で全無効。
- test_base_enabled_false_individual_enable_revives — 全無効でも下位 L4=True で個別有効。
- test_enabled_none_at_level_does_not_override — LevelSetting.enabled=None は enabled を
  上書きしない（None=継承）。

### TestSettingsRoundTrip（settings_to_dict ↔ settings_from_dict）
- test_base_and_enabled_round_trip — base/base_enabled の往復。
- test_levels_tuple_keys_are_restored — levels の tuple キーが復元（JSON は list 化）。
- test_unset_override_fields_stay_inherited_after_round_trip — 未設定 override は往復後も
  None（継承）。
- test_serialized_override_omits_none_fields — JSON 形式で override は非 None 項目のみ
  含む（levels は配列、key は list）。計画の JSON 形式。
- test_resolution_is_stable_across_round_trip — 往復後モデルが同じ解決結果を生む。

### TestFindOrphans
- test_returns_keys_absent_from_hierarchy — 現存しない levels キー（部品なし・pad 番号
  なし）を返す。IF の find_orphans。
- test_returns_empty_when_all_keys_exist — 全キー現存なら空。

## 現時点の状態（spec-first）

実行: `uv run pytest tests/pcbasm/pcb/test_grouping.py tests/pcbasm/pasting/test_settings.py`

- `src/pcbasm/pcb/grouping.py` は plan-implementer により既に存在。
  - TestPadShapeKey 6 件は全 green（PadShapeKey.of の契約は実装と一致）。
  - build_pad_hierarchy 系 11 失敗 + 4 error。**いずれも実装側の内部バグ**:
    `_build_root() takes 4 positional arguments but 5 were given`
    （src/pcbasm/pcb/grouping.py:219 付近、private 関数の引数不整合）。
    テスト側の問題ではない。`_build_root` の呼び出し/定義の引数を一致させれば解消する
    見込み（公開 IF には影響しない内部修正）。
- `src/pcbasm/pasting/settings.py` 未作成 → test_settings.py は ImportError で collection
  error（21 件は未収集）。実装後に評価される。

## 実装側に求める修正（plan-implementer 向け）

1. `src/pcbasm/pcb/grouping.py` の `_build_root()` の定義と呼び出しの引数数を一致させる
   （現状 4 受け取り・5 渡し）。これは public IF を変えない内部修正。
2. `src/pcbasm/pasting/settings.py` を IF どおり新規作成し
   `pcbasm.pasting` から import 可能にする（`__init__.py` export 追加）。
   - PASTE_OVERRIDE_FIELDS / PasteOverride / LevelSetting / ResolvedPaste /
     PasteSettingsModel / base_override_from_config / resolve_pad_settings /
     find_orphans / settings_to_dict / settings_from_dict。
   - 解決ルール: L0→L4 順に非 None override を上書き、enabled も非 None なら上書き
     （最具体が勝つ）。base/base_enabled が起点。
   - JSON: levels は配列、key は list、override は非 None 項目のみ。

これらはすべて公開 IF に厳密準拠したテスト。テストが正しい前提で実装を進めること。
仕様自体の欠落は見つかっていない。

## tests/helpers.py への追加

なし。Pad/Component/Polygon を直接構築。3rd-party モック・内部関数モックは未使用。

## 検証結果

- make format: pass（ruff/docformatter が両ファイルを整形済み）。
- pytest: grouping は PadShapeKey 6 green / hierarchy 系は実装内部バグで赤（spec-first
  として正常）。settings は実装未作成で collection error（spec-first として正常）。
