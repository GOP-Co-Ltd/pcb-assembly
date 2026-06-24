---
name: webui-thin-wrapper
description: WebUI（src/webui/）をロジックの薄いラッパーに保つ手順。計算/ドメインロジックを pcbasm コア層へ集約しエンドポイントで公開、JS は DOM/fetch/表示更新のみに限定する。WebUI の JS や router を書く/直す、ロジックが JS や router に漏れていないか点検する、サーバ resolved を返す API を設計するときに参照する。
---

# WebUI 薄ラッパー化

計算・ドメインロジックは pcbasm（`src/pcbasm/`）に集約し、WebUI（`src/webui/`）は
HTTP/WS/MJPEG の入出力変換と pcbasm 呼び出しに徹する。関連: CLAUDE.md「WebUI 設計」、
memory `feedback-webui-no-logic-in-js`、skill `webui-e2e`。

## レイヤ責務

- **pcbasm（`src/pcbasm/`）**: 計算・ドメインルール・解決の唯一の真実。装置非依存の純ロジック
- **routers（`src/webui/routers/`）**: pcbasm を呼び、resolved/集計を pydantic で公開。HTTP 例外への変換のみ
- **static/js**: fetch・DOM 操作・表示更新のみ

## 「ロジックが漏れている」サイン（点検チェックリスト）

- 階層/継承/override の解決を JS や router が計算している（pcbasm の `resolve_*` を使うべき）
- 正値・整数・enum 許容値などドメイン制約が JS や router に直書きされている
- サーバ応答を待たずローカル状態を楽観的に再計算している
- サーバが既に返している値（resolved 等）を JS で再導出している
- 幾何計算（座標変換・基板署名・キャリブ品質指標など）が router/job に直書きされている

## 許容（移さない＝描画専用）

- SVG 座標変換（`svgPoint`）、色補間（`routeColor`/`mixHexColor`）、極座標（`polar`）
- 判定: 「サーバの真実と一致すべき結果」か「ピクセル/色のための変換」か。後者は JS 可

## 移行手順（resolved 駆動への置換）

1. サーバが解決値/集計/算出を返すか確認。無ければ pcbasm の既存解決関数を再利用し、
    router の pydantic モデルにフィールドを足す（投機的な部分パッチ API は足さない）
2. ドメイン計算は pcbasm に公開メソッド/関数で置く。バリデーションは None 返却型
    （`str | None`）にして router 側で `HTTPException` に変換する
3. JS の再計算関数を削除し、レスポンスのフィールドを直接表示に流す
4. 編集系は `affected_*` レスポンス、または対象 config の再取得で表示更新（楽観更新は廃止）

## クライアント検証の線引き

- 残す（UX 最小限）: 空欄なら送らない、`Number.isFinite` でのパース可否、未接続なら送らない
- 消す（ドメインルール）: 正値・整数・enum・閾値（Python に一元化し、サーバの 400 を表示）

## 検証

- `make test-no-hardware`（router の resolved/集計/バリデーションテスト、pcbasm の純ロジック契約テスト）
- `make test-e2e` / `make webui-fake`（ブラウザで表示が API 由来であることを確認）。詳細は skill `webui-e2e`
