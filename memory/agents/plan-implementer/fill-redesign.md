# fill ロジック再設計 — Phase 1 実装ノート（plan-implementer）

契約メモ（正典）: `memory/agents/implementation-planner/fill-redesign.md`
本ノートは src/ 側実装の判断ログ・ジグザグ詳細・PCB フィクスチャ生成方法・未解決点。

## 変更ファイル一覧（src/ + data/ のみ。tests/ は未編集）

- `src/pcbasm/pasting/fill_path.py` — 全置換（螺旋系8ヘルパー撤廃 → 面/線/点フォールバック）
- `src/pcbasm/config.py` — `PasteDispenser` に `bead_width_factor/overlap/boundary_margin`（末尾デフォルト付き）
- `src/pcbasm/session.py` — `make_applicator` で3値を `PasteApplicator` へ配線
- `src/pcbasm/pasting/applicator.py` — `__init__` に3引数（`_` private）、`_fill` を成分ループ化、docstring 更新
- `src/scripts/dev/fill_path_simulate.py` — 戻り値三重リスト追従・成分単位描画・被覆率注記
- `src/scripts/dev/make_fill_coverage_pcb.py` — 新規。要件網羅フィクスチャ生成スクリプト
- `data/testing/fill_coverage/fill_coverage.kicad_pcb` — 新規。生成済みフィクスチャ

## ジグザグ実装の要点（`_outline_and_zigzag` / `_zigzag_rows`）

- 外周は `_ring_coords(component)`、内部は `minimum_rotated_rectangle` の最長辺を走査方向 u、
  短辺を行送り方向 v とし、`n_rows = max(1, int(short_len/line_spacing))` 本のスキャンラインを
  v 方向に等間隔（両端半間隔内側）で配置。
- 各スキャンライン ∩ component を `_scanline_intervals` で `LineString`/`MultiLineString` 分岐し、
  端点を u 射影座標でソートして `(start,end)` 区間化。区間自体も u 座標でソート。
- 行ごとに偶奇で向き反転（牛耕式）。`_outline_and_zigzag` で外周→各行を最近傍向き合わせで連結。

### 凹成分の横断対策（契約メモ §2 の「出たケースだけ局所対処」を実装）

- 計画通り最小実装でまず作ったところ、**plus（十字）形**で行間接続が成分外を横切るケースを自前検出。
  → `_connect_via_outline` を追加（局所対処）。接続ジャンプ `LineString([start,end])` が
  `component.buffer(1e-9).covers(...)` を満たさないときのみ、外周頂点列上で start/end 最近傍頂点間を
  時計回り/反時計回りの短い方で辿る中継点を挿入。covers を満たす通常ケースはノーオペで素通り。
- 検証: 大矩形/roundrect/L字/ダンベル/U字/plus/cross すべてで隣接セグメント `covers` 違反 0。
  ダンベルは nozzle=2.0 で 2 成分に分割（成分跨ぎ横断セグメント無し）を確認。
- L字では外周経由の連結線が1本入る（boustrophedon の腕跨ぎ）。ポリゴン内には収まる（containment OK）。
  パスはやや長くなるが「先回り一般化しない」方針に従い最小対処に留めた。

## total_amount 配分（契約メモ §4 決定事項A）

- `total_amount = polygon.area * ul_per_mm2` を `len(components)` で均等配分。実装はメモのコード片通り。
- 単一成分パッドで旧挙動と完全一致。spec の `TestMultiComponentPad`（N成分=N本・各 total/N・send_gcode N回）が pass。

## PCB フィクスチャの作成方法

- **pcbnew API で生成**（環境で `import pcbnew` 可）。スクリプト `make_fill_coverage_pcb.py` を残置。
  再生成: `uv run python -m scripts.dev.make_fill_coverage_pcb`
- 60x40mm の Edge.Cuts 矩形 + F.Paste パッド8個（footprint 各1パッド）:
  - 面: 大矩形(12x8) RECT / roundrect(10x7 r2) ROUNDRECT
  - 追加: SPLIT_DB（細首ダンベル, ネック幅0.3mm, x=18 y=36）→ d=0.4(inset=0.2) で `buffer(-inset)` のネックが
    途切れ **2成分に分裂**。`-d 0.4` の既定コマンドで成分分割を目視できる（PNG で2ブロックが独立ジグザグ・接続線なし）。
  - 凹形: L字 / ダンベル → `PAD_SHAPE_CUSTOM` + `AddPrimitivePoly(F_Paste, SHAPE_POLY_SET, 0, True)`
  - 線: 細長矩形(8x0.3) → buffer(-inset) 空で線フォールバック
  - 点: 極小(0.2x0.2) → 点フォールバック
- pcbnew API 注意点（ハマり所）:
  - `SetAnchorPadShape(layer, shape)` / `AddPrimitivePoly(layer, poly, thickness, filled)` は
    **第1引数に layer（F_Paste）が必須**。
  - `FromMM` の pyright stub が union 返しのため `_mm` で `cast(int, ...)` が必要。
- フィクスチャ確認: `PcbFile` で7パッド読込 OK。simulate を nozzle=1.0 で実行し PNG 目視（面/凹/線/点すべて妥当、被覆率 99〜100%）。
  - ※ custom pad のアンカー極小円が CONCAVE_L で別パッド（area≈0.01）として現れる（凹の切欠きに中心があるため）。
    点フォールバックを正しく辿るので実害なし。気になれば別タスクでアンカー位置を形状内に寄せる。

## 検証結果

- `make format`: パス（一時的に test 側 parallel 編集で Failed が出たが src 由来ではない。最終はクリーン）
- `make type`（pyright）: 0 errors
- `make test-no-hardware`: 490 passed, 15 deselected（spec-test-author のテストと合流済み）

## 契約メモとの不整合・疑問点

- なし。シグネチャ・戻り値型・バリデーション substring・配分・成分ループはメモ通り。
- 1点だけメモ範囲外の追加: `_connect_via_outline`（凹横断の局所対処）。メモ §2 が明示的に許可した
  「外周経由で繋ぐ」局所対処であり、先回り一般化はしていない。code-simplifier に L字の連結線長を
  さらに詰める余地があるかレビュー依頼可（必須ではない）。
