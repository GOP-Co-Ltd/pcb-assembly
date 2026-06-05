# はんだ fill ロジック再設計 — セッション引き継ぎメモ

作成日: 2026-06-05 / 前セッションが並列ツール呼び出しの記法バグで継続困難になり引き継ぎ。

## 現在地（重要）

- **承認済み計画**: `/home/gop/.claude/plans/fill-adaptive-parasol.md`（全文必読。これが正典）
- **作業ブランチ**: `feature/20260605/fill-outline-zigzag`（`main` から分岐済み・切替済み・未コミット）
- **進め方**: エージェントチーム（`agent-team-startup` skill 準拠）。実機テストは Claude が実行せず人間が行う。
- **次のアクション**: Phase 0（`implementation-planner` で契約メモ確定）から着手。下記「次にやること」参照。

## やること要約（螺旋 → 外周＋ジグザグ への全面再設計）

`src/pcbasm/pasting/fill_path.py`（430行・公開API `build_paste_fill_path` のみ）の螺旋アルゴリズムを撤廃し、以下に作り直す。**シンプルさ最優先**。

### 新公開シグネチャ（ユーザー確定・破壊的変更）
```python
def build_paste_fill_path(
    polygon: Polygon,
    nozzle_diameter: float,
    *,
    bead_width_factor: float = 1.0,
    overlap: float = 0.0,
    boundary_margin: float = 0.0,
) -> list[list[Point2d]]:   # 旧 list[Point2d] → 成分別ポリラインのリスト
```

### フォールバック階層（自然な構造にする）
```
面塗布(_area_fill) → 空なら → 線塗布(_line_fill) → 空なら → 点塗布(_dot_fill)
```
- 面: `polygon.buffer(-inset)` の各連結成分ごとに「外周トレース＋内部ジグザグ（牛耕式・最長軸方向スキャンライン）」。各成分=1ポリライン。
- 線: 最長軸の中心線2点（既存 `_generate_linear_path` 流用、`end_inset` 補正、長軸≤2*end_inset で空）。`[[start,end]]`。
- 点: `representative_point()` の1点 `[[rep]]`。**極小でも必ず1点塗布、スキップ（空リスト）廃止**。

### 数式
```
w(bead_width) = nozzle_diameter * bead_width_factor
line_spacing  = w * (1 - overlap)
inset         = boundary_margin + w/2     # 面塗布領域 = polygon.buffer(-inset)
end_inset     = boundary_margin + w/2
```
`k_b=1, ov=0, m=0` で現状と幾何整合。

### バリデーション
不正引数（nozzle≤0, overlap∉[0,1), boundary_margin<0）→ `ValueError` / 空・不正ポリゴン → `[]`。

### 撤廃する螺旋ヘルパー
`_generate_spiral_path` / `_spiral_one_component` / `_walk_rings_outward` / `_estimate_ring_spacing` / `_truncate_ring` / `_rotate_ring_to_nearest` / `_connect_nearest` / `_sort_components_by_seed`
### 流用
`_offset_components`（連結成分抽出）/ `_ring_coords` / `_generate_linear_path`
### 新規
`_area_fill` / `_outline_and_zigzag` / `_line_fill` / `_dot_fill`

## ユーザーが下した設計判断（合意済み・蒸し返さない）

1. アルゴリズム: 螺旋 → **外周＋ジグザグ**（最長軸方向スキャンライン）
2. 点塗布: **必ず1点塗布**（スキップ廃止）
3. 凹形: **連結成分ごとに分割**、成分間は `FillSequence` の lift→travel→plunge 空中移動。戻り値型を `list[list[Point2d]]` へ（破壊的）
4. 被覆パラメータ: **overlap / boundary_margin / bead_width_factor を導入**、config/session 経由で配線
5. `total_amount` は**面積ベース維持**（`polygon.area * ul_per_mm2`）。margin 薄塗りは人間が実機評価。塗布面積追従は本タスク外
6. **要件網羅 PCB フィクスチャ**（`.kicad_pcb`）を作り、`fill_path_simulate.py` で全要件を1枚PNGで目視
7. エージェントチーム＋フェーズ別ブランチ

## Plumbing（配線）

- `src/pcbasm/config.py:25` `PasteDispenser`（attrs.frozen）に `bead_width_factor=1.0 / overlap=0.0 / boundary_margin=0.0` 追加（デフォルト付きでTOML後方互換）
- `src/pcbasm/session.py:107` `make_applicator` で各値を `PasteApplicator` へ
- `src/pcbasm/pasting/applicator.py:68` `__init__` に kw 引数追加（`_` private 属性）
- `src/pcbasm/pasting/applicator.py:196` `_fill` を**成分ループ化**: 各ポリライン→`Path`(3D化+transform)→`FillSequence`、成分間 lift→travel→plunge で連続GCode送信。total_amount は面積ベース維持

## テスト（tests/pcbasm/pasting/test_fill_path.py 全面改訂）

`testing-strategy`: fill_path は HW非依存の**純粋ロジック＝unit区分**（モック不要・実Polygon直接・@mark_hardware不要）。公開API経由・`class TestXxx`集約・例外はsubstring検証・ミラーレイアウト。
- `TestFallbackHierarchy`（面/線/点を形状×ノズル径で誘発、点で空にならない）
- `TestAreaFill`（外周ポリライン存在・ジグザグ最長軸方向・被覆均一）
- `TestSegmentContainment`（**最重要・現状の盲点**: 各成分ポリラインの隣接セグメントが `polygon.covers(LineString)`、L字・ダンベル parametrize、成分間に面上接続線なし）
- `TestCoverage`/`TestExteriorMargin`（overlap で間隔縮む・margin 確保。しきい値は parametrize で導出）
- `TestInvalidInput`/`TestReturnType`（戻り値 `list[list[Point2d]]` へ更新、新パラメータ不正値 ValueError）
- 公開API契約ピン（戻り値型破壊変更。必要なら `tests/pcbasm/test_api_contract.py` 新設 + `@pytest.mark.api_contract`）
- `tests/pcbasm/pasting/test_applicator.py` を成分ループ・新戻り値型に追従

## 要件網羅 PCB フィクスチャ ＋ 最終動作テスト（ユーザー追加タスク）

`fill_path_simulate.py` の事実（前セッションで Explore 確認済み）:
- `src/scripts/dev/fill_path_simulate.py:225-227` `_build_paths` が各パッドに `build_paste_fill_path(pad.polygon, nozzle_diameter)` を呼ぶ → 戻り値型変更で「パッド×成分」二重リストに追従必要
- 入力: `src/pcbasm/pcb/kicad.py:33` `PcbFile(pcb_path)`（KiCAD 9.0 pcbnew API、`.kicad_pcb`、`pad.polygon` は Shapely）。F.Paste/B.Paste レイヤのパッドを抽出
- CLI: 位置引数 `pcb_file`、`--nozzle-diameter/-d`（既定0.4）、`--output/-o`、`--layer top|bottom`
- 出力: matplotlib PNG（pad polygon, fill path のハロ＋中心線＋始点マーカー）
- 既存サンプル: `data/testing/led_blinker/led_blinker.kicad_pcb`、`data/TJ-56-67/TJ-56-67.kicad_pcb` 等。**custom shape pad は未使用**

作るもの: `data/testing/fill_coverage/fill_coverage.kicad_pcb`（pcbnew API 生成スクリプト or 手書き s-expr）。F.Paste レイヤに:
- 面塗布: 大きめ矩形・roundrect
- 凹形: L字・ダンベルを `smd custom` ＋ `(primitives (gr_poly (pts (xy ...))))` で定義
- 線塗布: 細長矩形（buffer(-inset) 空）
- 点塗布: 極小パッド
最終確認: `uv run python -m scripts.fill_path_simulate data/testing/fill_coverage/fill_coverage.kicad_pcb -d 0.4 -o /tmp/fill_coverage.png` で全フォールバック・凹形成分分割・被覆を目視。

## 次にやること（このメモを読んだ新セッションの手順）

1. `/home/gop/.claude/plans/fill-adaptive-parasol.md` 全文を読む（正典）
2. ブランチ確認: `git branch --show-current` → `feature/20260605/fill-outline-zigzag`
3. **Phase 0**: `implementation-planner` agent を起動し、公開IF契約を `memory/agents/implementation-planner/fill-redesign.md` に確定（シグネチャ・契約・実装ステップ・テスト観点・consumer IF変更通知）。コードは書かせない
4. **Phase 1 並列**: 契約確定後、`spec-test-author`（tests/ 専用）と `plan-implementer`（src/ 専用）を**1メッセージで並列起動**（disjoint、agent-team-startup パターンA）。中間メモは `memory/agents/<agent>/fill-redesign.md`
5. 合流: `make format && make type && make test-no-hardware`。spec の意図通り pass/fail か確認
6. PCB フィクスチャ作成 + `fill_path_simulate.py` 追従 → 最終動作テスト（/tmp/fill_coverage.png 目視）
7. `code-simplifier`（外周＋ジグザグのコアを最小コードに）→ `docs-keeper`（docstring/dev script README）
8. 各フェーズ DoD: `make format && make type && make test` グリーン。実機検証はユーザーが行う

## ⚠ 前セッションの失敗モード（新セッションへの注意）

前セッションは**1メッセージに複数の tool_use を並べる際、2つ目以降で `antml:` 名前空間プレフィックスを落としてしまい**、ツール呼び出しが "malformed and could not be parsed" になって繰り返し失敗した。**並列発火する場合も各 invoke/parameter タグに正しいプレフィックスを付けること。** 不安なら逐次（1メッセージ1ツール）で確実に進める。agent-team の並列起動は重要だが、記法が崩れるくらいなら逐次でもよい。

## 参照 skill / 規約
- `agent-team-startup`（標準サイクル・並列化判断パターンA）
- `testing-strategy`（unit区分・モック禁止範囲・ミラーレイアウト・契約ピン例外）
- `refactor-conventions`（カプセル化 `_` prefix・None返却 vs ValueError・class TestXxx集約）
- `maximize-parallels`（tool呼び出しレベルの並列）
- `memory/agents/README.md`（中間メモの書式）
- Git: `main` 直接コミット禁止、コミットは `<種別>(<スコープ>): <内容>`、検証通過前コミット禁止
