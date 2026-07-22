# orchestrator ノート: region-pad-align

計画書: `memory/agents/orchestrator/region-pad-align-plan.md`（plan mode で Plan agent 2 本 + ユーザー確認を経て承認済み）
ブランチ: `feature/20260722/region-pad-align`

## 委譲判断・計画外の決定

- **implementation-planner は省略**: 計画は plan mode 内で Explore 3 本 + Plan agent 2 本（領域分割アルゴリズム / 実装変更計画）を経てユーザー承認済み。二重計画は冗長と判断
- **領域サイズは camera.crop 由来**（ユーザー確定）: 当初推奨は新設定 region_size だったが、「crop はレンズ歪みの起こらない信頼範囲を捉えている」というユーザーの運用意図により crop.size ÷ pixel_per_mm 導出へ変更
- **kurousagi camera.crop 600→300 の未コミット変更**（ユーザーの実機チューニング）: ユーザー確認の上、本ブランチの独立コミット 09bbd62 として取り込み
- **設計 agent 間の裁定**（アルゴリズム設計 vs 実装計画で分かれた点）:
  - グリッド原点: board 原点固定を採用（pads bbox 起点は有効 pad 切替でタイルがずれるため却下）
  - ROI: 領域矩形（region.box）を採用（pad copper bbox 案は巨大 pad で無制限 ROI が再発するため却下）
  - lookup: Pad の attrs 同値比較を採用（(designator, pad_number) は KiCAD 上一意でないため却下）
  - rename 最小化: PadAligner / PadAlignmentSession / PadAlignmentResult は名前維持
- 巨大 pad の境界セル再割当てルールを採用（サーマルパッドでセルが銅箔内部に沈み照合不能 → max_failures=0 で即中止する footgun の回避）

## 並列走行の同期点

公開 IF は計画書の「凍結する公開 IF」節でシグネチャ確定済み。spec-test-author は tests/ のみ、plan-implementer は src/ + configs/ のみ（disjoint）。

## レビュー裁定ログ

code-reviewer verdict: **approve**（must-fix 0 / should-fix 1 / nit 4）。詳細: memory/agents/code-reviewer/region-pad-align.md

- should-fix「_validate_region_fits_frame の非正方形 crop 一般化が数学的に不正（per-axis size×ρ ではなく w|cosθ|+h|sinθ| / w|sinθ|+h|cosθ| が正）」→ **採用**、code-simplifier に委譲。orchestrator 自身の diff 確認でも同一指摘。現行 configs は正方形 crop のため実影響ゼロだがマージ前に修正
- nit「render.py の最小 ROI が実機で 1.0→3.0mm に変わる」→ **採用**（挙動保存優先: 実機 configs は旧 min_roi=1.0 を renderer に渡していた）。定数を 1.0 に変更、code-simplifier に委譲
- nit「posctrl/README.md の旧シンボル残」→ docs-keeper に委譲
- nit「align() 側 ValueError の直接テスト無し」→ **却下**: セッション構築時検証のテストで仕様は固定済み、align 側は防御的バックストップでありテスト追加は過剰（テスト方針: 必要十分なテストのみ）
- nit「未追跡ゴミファイル \0014...」→ 触らない（本タスク以前から存在、要求外の掃除はしない）。ユーザーに報告のみ
- 計画外判断 3 件（PadAligner.__init__ の image_size/search_window_px、render 定数化、config_store ラベル）: reviewer・orchestrator とも妥当と判断し **承認**
