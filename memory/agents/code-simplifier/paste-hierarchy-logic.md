# paste/hierarchy 純ロジックの簡素化

対象: Phase 1 新規 2 ファイルの内部簡素化（公開 IF 維持）。

- `src/pcbasm/pcb/grouping.py`
- `src/pcbasm/pasting/settings.py`

## 簡素化した内部実装

### grouping.py — 階層構築を再帰グループ化に統一

`build_pad_hierarchy` の階層構築を、レベル別の4つの builder 関数から
1つの汎用再帰関数に置き換えた。

削除した private:

- `_build_root` / `_build_package_node` / `_build_designator_node` /
  `_build_shape_node`（4関数、計 ~115 行）
- `_ordered_unique`（出現順ユニーク化ヘルパ。dict の挿入順保持で代替）

追加した private:

- `_build_node(level, key, pads, keys_by_pad)` —
  各ノードを構築し、配下を「次レベルの key」で `dict.setdefault` により
  グループ化して再帰する。L4 で子なし。
- `_node_label(level, key, pad)` — ラベル導出
  （L0=`"全部品"`、L4=`f"{designator}.{pad_number}"`、他は key 末尾）。

`build_pad_hierarchy` 本体も、中間マップを 5 個から 2 個に削減:

- 残したのは `keys_by_pad`（`PadHierarchy` の実体）と `ordered_pads`。
- 削除: `package_by_pad` / `shape_label_by_pad` / `pads_by_package`。
  ツリーは `keys_by_pad` から直接 group-by できるため不要だった。

要点: 旧コードは各レベルで pad をリスト内包で再フィルタし、順序も
`_ordered_unique` で再導出していた。新コードは `keys_by_pad` が持つ
L0–L4 キー列をそのまま prefix tree として 1 パスで畳み込むので、
「同じ階層を別の場所で 2 度表現する」重複が消えた。`node_keys_for_pad`
が返す 5 キーとツリーの key が定義上同一になることを利用している。

### settings.py — 変更なし

override マージ（`_apply_override`）/ JSON 変換（`_override_to_dict` /
`_override_from_dict`）/ `_as_sequence` はいずれも単一責務で短く、
`PASTE_OVERRIDE_FIELDS` を正準順として使い回す形になっており十分明瞭。
cattrs 化は tuple キー・None 欠落表現の都合でむしろ冗長になるため不採用。
手書き JSON 変換のままが最小。よって外科的に「変更なし」と判断。

## 公開 IF 維持の確認

- クラス/関数/引数/戻り値型は不変。`PadShapeKey` / `PadHierarchyNode` /
  `PadHierarchy`（`all_keys()` 含む）/ `build_pad_hierarchy` /
  `DEFAULT_SHAPE_QUANTUM` / `HierKey` すべて据え置き。
- `_hier_keys_for` は元から private のため、削除した builder 群は
  すべてモジュール内部に閉じており export に影響なし。
- `pcb/__init__.py` / `pasting/__init__.py` の export は無変更。
- 出現順・ラベル文字列・`pads` 集約はテストで固定されており全 pass。

## 簡素化できなかった部分・理由

- `_node_label` の空 pads ガード（`pads[0] if pads else "全部品"`）は、
  pad ゼロ件時に root(L0) のみ空ノードになるケースのため残置。
  子ノードは非空グループからのみ生成されるので L0 以外は空にならない。

## 検証結果

- make format: pass
- make type (pyright): pass（0 errors, 0 warnings）
- 対象テスト `tests/pcbasm/pcb/test_grouping.py` +
  `tests/pcbasm/pasting/test_settings.py`: 40 passed
- 念のため `tests/pcbasm/pcb` + `tests/pcbasm/pasting`（-m "not hardware"）:
  188 passed
