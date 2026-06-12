# WebUI Phase 2: FrameHub + MJPEG preview（code-simplifier）

結論: **変更なし**。

対象（feat 2 コミット 40cefc2 / 4e48238 の追加分）:
`src/pcbasm/hal/framehub.py`, `src/webui/preview.py`, `src/webui/fake_camera.py`,
`src/webui/routers/preview.py`, `src/webui/state.py`

## 検討した候補と見送り理由

| 候補 | 見送り理由 |
| --- | --- |
| `_CircleRenderer` / `_CopperRenderer` の検出間引き（`_next_detect` 4 行 × 2）の共通化 | 2 用途のための新規抽象。行数・理解コストが純減しない |
| `_CircleRenderer` の `detector is None` 分岐を `_build_renderer` へ移動 | circle 描画コードが 2 箇所に分散し凝集性が悪化 |
| `framehub.latest()` の述語と `_wait_for` 内 `_frame is not None` ガードの重複解消 | `lambda _: True` 化は意図の可読性を下げる |
| `framehub._stop_event` の Optional 管理縮約 | スレッド停止機構。冪等 stop / 再 start の正しさが per-start Event に依存（制約: 確信なき場合は触らない） |
| `routers/preview._ClosingStreamingResponse` / `_close_when_suspended` の整理 | plan-implementer が実 uvicorn で検証済みの切断時クリーンアップ（starlette sync iterator GC 問題対処）。触らない |
| `state.py` / `fake_camera.py` | 追加分は各数行・ロック保護範囲も最小で余地なし |

Phase 2 追加分は Phase 1 の simplifier 整理（54657e4）後の規約に沿って実装されており、
「明確に簡素化された」と説明できる変更が無いため何も変更していない。

## 検証（ベースライン確認）

- `uv run pyright src/`: 0 errors / 0 warnings
- `uv run pytest tests/pcbasm/hal/test_framehub.py tests/webui -m "not hardware" -q`: 144 passed
