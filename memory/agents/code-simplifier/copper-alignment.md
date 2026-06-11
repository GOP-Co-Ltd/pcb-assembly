# code-simplifier: copper-alignment 簡素化レビュー

## 結論: 変更なし

対象 3 ファイル（`src/pcbasm/posctrl/copper.py`、`src/pcbasm/posctrl/__init__.py`、
`src/scripts/posctrl/copper_detection.py`）を計画書・実装ログ・テストと突き合わせて
レビューした結果、簡素化すべき箇所なしと判断した。

## 検討した候補と却下理由

1. **`_pixel_of` のインライン化**（copper.py）
   `_board_to_pixel_affine` からの 3 回呼び出しのみだが、投影公式
   `pixel = center + ppm·R(s − T_b(b))` との対応が 1 箇所に読めるため維持。
2. **`match()` の二重空チェック**（observed 先頭 / template edge_count）
   それぞれ早期 return で distanceTransform / matchTemplate の無駄を回避し、
   テスト契約（observed 空・expected 空とも None）に直接対応。過剰防御ではない。
3. **`_template_rect` のクランプ**
   計画書の落とし穴「matchTemplate は探索領域 < テンプレートで例外」への必須対処。
4. **fill_mask の polygon 単位 OR 合成**
   一括 2 パスへの単純化は入れ子島（穴内の孤立パッド）を消す機能退行
   （plan-implementer ログ §1 で根拠確定、インラインコメントあり）。
5. **デモ `_render` の overlay 合成**
   `overlay.copy() + addWeighted` 方式に書き換え可能だが行数・明瞭さとも同等で
   「ただ違うコード」にしかならない。
6. **`_run_interactive` の構造**
   線形フローでヘルパー粒度も適切。`anchor` / `board_pos` は
   計画書の最重要設計判断（投影アンカー固定）を担うため削減不可。

未使用 import・dead code・orphan なし。private 規約・attrs 流儀・既存スタイルに整合。

## 検証

`make format && make type && make test-no-hardware` 全グリーン
（pre-commit 全 Passed、pyright OK、526 passed / 15 deselected）。コミットなし。
