---
name: webui-thin-wrapper
description: WebUI（backend src/web/api/ と frontend src/web/ui/）をロジックの薄いラッパーに保つ手順。計算/ドメインロジックを pcbasm コア層へ集約しエンドポイントで公開、frontend の page ハンドラは backend の JSON をテンプレへ渡すだけ、JS は DOM/fetch/表示更新のみに限定する。WebUI の JS や router や page ハンドラを書く/直す、ロジックが漏れていないか点検する、サーバ resolved を返す API を設計するときに参照する。
---

# WebUI 薄ラッパー化

計算・ドメインロジックは pcbasm（`src/pcbasm/`）に集約し、WebUI は入出力変換と pcbasm
呼び出しに徹する。WebUI は **2 プロセス**（backend WebAPI = `src/web/api/`、
UI frontend = `src/web/ui/`）なので、薄く保つ対象が 3 層ある。関連: AGENTS.md「WebUI 設計」、
skill `webui-e2e`。

## レイヤ責務

- **pcbasm（`src/pcbasm/`）**: 計算・ドメインルール・解決の唯一の真実。装置非依存の純ロジック
- **backend routers（`src/web/api/routers/`）**: pcbasm を呼び、resolved/集計を pydantic で公開。HTTP 例外への変換のみ
- **frontend page ハンドラ（`src/web/ui/pages.py`）**: `MachineClient` で backend から取った
    pydantic モデルをテンプレートへ渡すだけ。装置の状態を持たず、`config/` も読まない
- **static/js**: fetch・DOM 操作・表示更新のみ

## 2 プロセスで増える線引き

- **frontend は backend の値を再計算しない。** SSR ページに要る値は backend の
    エンドポイントに足して取得する（frontend で導出すると backend 直叩きと表示が食い違う）
- **表示文字列の組み立てはサーバ側。** マシンの表示ラベルは `MachineEndpoint.label`、
    タブ / feature の表示名は `web.ui.layout` に置く。テンプレートや JS で組み立てない
- **frontend が持ってよい知識**は「どのページにどのテンプレートと、どの backend 取得が
    必要か」まで（`web.ui.layout` の TAB/FEATURE テーブル、`pages.py` の取得対象の選別）
- **中継は素通し。** `/m/{machine_id}/api/` 以下を扱う proxy（`src/web/ui/proxy.py`）は
    ヘッダ組み立てとストリーミングだけを行い、ボディを解釈・加工しない

## 「ロジックが漏れている」サイン（点検チェックリスト）

- 階層/継承/override の解決を JS や router が計算している（pcbasm の `resolve_*` を使うべき）
- 正値・整数・enum 許容値などドメイン制約が JS や router に直書きされている
- サーバ応答を待たずローカル状態を楽観的に再計算している
- サーバが既に返している値（resolved 等）を JS で再導出している
- 幾何計算（座標変換・基板署名・キャリブ品質指標など）が router/job に直書きされている
- frontend の page ハンドラが backend のレスポンスから値を導出・集計している
- 表示文字列をテンプレートや JS で組み立てている（サーバが `label` 等で返すべき）

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

- `make test-no-hardware`（`tests/web/api/` の resolved/集計/バリデーション、`tests/web/ui/` の
    SSR ページと中継、pcbasm の純ロジック契約テスト）
- `make test-e2e` / `make api-fake` + `make ui-fake`（ブラウザで表示が API 由来であることを確認）。
    詳細は skill `webui-e2e`
