# WebUI Phase 2: FrameHub + MJPEG preview（plan-implementer）

担当: `src/pcbasm/hal/framehub.py` + `hal/__init__.py` export、`src/webui/` 一式（preview / fake_camera / routers/preview / state / settings / app / templates / preview.js / css）。
`tests/` と `data/testing/webui/fake_camera.png` は spec-test-author 担当（本 agent は未編集）。

## 計画外の判断ログ

1. **`mjpeg_stream` の戻り型を `Iterator[bytes]` → `Generator[bytes]` に変更**
   - 理由: 消費側（router の確実な close、spec-test-author のテスト `gen.close()`）が
     `close()` を必要とする。実体は元からジェネレータで、ランタイム挙動は不変。
     spec-test-author のテストもこの型を前提に書かれており契約として整合済み。
2. **`routers/preview.py` に `_ClosingStreamingResponse` を追加**（計画に無い私的クラス）
   - 発見した問題: starlette（uvicorn は ASGI spec 2.3）はクライアント切断時に
     sync iterator を close せず **GC 任せ**。素の `StreamingResponse` だと切断後も
     参照カウントが下がらず FrameHub が止まらない（解放まで数秒〜不定）。
   - 対処: `stream_response` を override し、finally（shield 付き）で
     ジェネレータを確実に close。worker thread が next() 実行中
     （`ValueError: generator already executing`）の場合は yield 停止を待って
     再試行（上限 10s > capture timeout 5s）。
   - 実 uvicorn で検証済み: 接続中 `preview_clients=1` → 切断後 4 秒以内に 0、
     FrameHub started/stopped ログが対で出る。
3. **`src/webui/__main__.py` に `setup_logging(logging.INFO, namespaces=("pcbasm", "webui"))` を追加**
   - 理由: uvicorn の既定ログ設定では pcbasm 名前空間のログが出ず、
     計画の E2E 手順 4（`grep -i framehub server.log`）が成立しないため。
4. **preview_pane に `data-stream-url="/api/preview/stream"` を埋め込み**
   - spec-test-author のページテスト（HTML に `/api/preview/stream` マーカー）と
     E2E 手順 1 に整合させた。preview.js は data 属性から URL を取る。
5. FrameSource は hub の private に触らない構造にした（`subscribe()` が
   camera と `_wait_next` バインドメソッドを渡す）。pyright の
   reportPrivateUsage warning ゼロを維持するため。公開 IF は計画どおり。

## 他 implementer / spec-test-author への IF 変更通知

- `PreviewService.mjpeg_stream(...) -> Generator[bytes]`（旧計画: `Iterator[bytes]`）。
- **TestClient（starlette 1.3）は無限 StreamingResponse をストリーム読みできない**:
  `client.stream()` でも portal が app を完走させてから応答するため、
  `/api/preview/stream` の本文を TestClient で読むテストはハングする。
  ストリーム内容の検証は `PreviewService.mjpeg_stream` 直接消費
  （`next()` × N → `close()`）か実 uvicorn + curl で行うこと
  （spec-test-author は既にこの方針でテストを書いており問題なし）。

## 既知の制約・残課題（Phase 3+ へ引き継ぎ）

- `Camera.close()` は追加していない（ユーザー決定）。rebuild/shutdown は
  hub.stop + 参照破棄（GC）。実機でマシン切替を高頻度に行うと picamera2 の
  二重 open が起き得る（計画リスク 1）。
- snapshot はオーバーライドスロットを見ない（生フレーム + overlay のみ）。
  計画どおりだが、Phase 3 でジョブ注釈画像を snapshot にも出したければ要変更。
- MJPEG 接続が残っていると uvicorn の graceful shutdown が待たされ得る
  （計画リスク 2。`--timeout-graceful-shutdown` 未設定）。
- 検出間引きキャッシュはストリームごと（計画どおり）。複数クライアントで
  同一 overlay の検出が多重実行される。

## 検証結果

- make format: pass
- make type（pyright）: pass（0 errors / 0 warnings）
- pytest -m "not hardware"（全体）: pass（740 passed。tests/webui 133 +
  test_framehub 17 を含む）
- make test の hardware 区分（test_framehub の実カメラスモーク等）: **ユーザー実行待ち**
- E2E（実 uvicorn + FakeCamera, port 8093）: snapshot 4 overlay JPEG 復号 OK /
  MJPEG multipart 27〜29 frames 復号 OK / copper 緑画素 OK / override スロット OK /
  preview_clients 1→0 OK / FrameHub started/stopped ログ対 OK / overlay=bogus 422 OK /
  canny 保存で test-fixture machine.toml の 2 行のみ変化（コメント保持、検証後 git checkout で復元）/
  camera.* PUT で hub 再構築 OK / マシン切替で hub 再構築 OK
