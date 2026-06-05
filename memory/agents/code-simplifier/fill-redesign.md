# fill 再設計 — code-simplifier ノート（外周＋ジグザグ簡素化）

対象: `src/pcbasm/pasting/fill_path.py`（螺旋撤廃後の新実装）。
契約: `memory/agents/implementation-planner/fill-redesign.md`（公開IF・配分・内包契約は不変）。
ベースライン/最終とも `make test-no-hardware` = **490 passed, 15 deselected**。

## 削った/まとめたもの

1. `_connect_via_outline` の閉環ガードを定数化。
   - 変更前: `vertices = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring` ＋ `if not vertices: return []`。
   - 変更後: `vertices = _ring_coords(component)[:-1]`（コメントで閉環不変条件を明示）。
   - 振る舞い不変の根拠: ここに到達するのは `_offset_components` が `is_valid and not is_empty` で
     選別した有効成分のみ。shapely の `Polygon.exterior.coords` は常に閉環（先頭==末尾, 5点以上）
     なので条件 `len(ring)>1 and ring[0]==ring[-1]` は恒真、`else ring` 分岐と `if not vertices`
     ガードは通常・凹いずれのケースでも到達不能（死にコード）。`[:-1]` は無条件に正しいスライス。

## 削らなかった候補と理由

- `_connect_via_outline` 本体（凹横断の局所対処）: `TestSegmentContainment.test_every_segment_is_covered_by_polygon`
  が依存する load-bearing。`covers` 判定で通常（凸）ケースは即 no-op 素通り（投機的作り込みではない）。
- forward/backward 両方向walk → 短い方採用: 削ると経路長が変わる＝振る舞い変化。簡素化ではないので不可。
- `_polyline_length` ヘルパー（2箇所利用）/ `_ring_segment`: 小さく明快で重複なし。インライン化は可読性低下のみ。
- `_zigzag_rows` の牛耕反転 `reversed([(b,a) for a,b in intervals])`: 端点swap＋区間順反転で蛇行を作る必須処理。
- `_scanline_intervals` の `LineString`/`MultiLineString`/else 三分岐: 凸=単線, 凹=複線, 点接触=else と全分岐到達。
- applicator `_fill`: 契約 §4 のコード片そのままで均等配分・成分ループとも最小。手を入れず。

## 検証

- `make format`: Passed
- `make type`（pyright）: 0 errors, 0 warnings
- `make test-no-hardware`: 490 passed, 15 deselected
- `scripts.dev.fill_path_simulate ... -d 0.4`: fill path 生成 8 / 8 成功

簡素化は 1 点（死にコード除去）のみ。コア（外周＋ジグザグ＋局所対処）は既に必要十分で、それ以上は
振る舞いを変えるため触らない判断。
