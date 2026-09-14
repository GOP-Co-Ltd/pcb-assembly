# ノズルクリーニングのテスト実行機能

ブランチ `feature/2026-09-14/nozzle-clean-test`（`main` = 01a8bd5 から分岐）。
solo-dev-cycle（実装は自分、レビューのみ `code-reviewer` へ委譲）。

## 段階 1: 計画

### 要件

ノズル位置ページのクリーニング節から、クリーニング動作を試しに実行できるようにする。
押し込み量・こすり幅を追い込むとき、塗布ジョブを 1 本通さずに効き具合を見られるようにするため。

### 採用した設計

| 項目 | 決定 | 却下した案と理由 |
| --- | --- | --- |
| 実行経路 | 同期エンドポイント `POST /api/pasting/nozzle-clean/test` | **ジョブ化**: ノズル位置ページは `JOB_TEMPLATES` に無い純 feature ページで、ジョブにするとジョブコンソール・パラメータフォーム・`_JOB_FEATURE_CONTEXT` まで抱き込む。動作は 20 秒程度で進捗表示も中断も要らない |
| 先例 | `machine_control` の `move_to_cap`（操作権 + `machine_lock` + 同期送信 + `wait_for_done`） | — |
| パージ | ジョブと同一シーケンス（パージ込み）。ユーザーが選択 | **パージ有無のチェックボックス**: ノブが増える。**パージなし**: パージ工程そのものを確認できない |
| リトラクト | `clean_nozzle` の直後に `applicator.retract()` | `clean_nozzle` 側に入れる案は却下（塗布ジョブのプライム収支が崩れる。既存契約を変えない） |
| 応答 | `clean_nozzle` が `log` に渡す実施内容 1 行をそのまま返す | JS で組み立てる案は却下（webui-thin-wrapper: 表示文字列はサーバー側） |

### 検査の順序（この順でないと誤ったエラーになる）

1. `machine_lock` を取る（ジョブ実行中なら 409。「未記録」より「使用中」のほうが行動可能）
2. `state.nozzle_clean()` が None → 400「ノズルクリーニング位置が未記録です」
   （Klipper に触れる前。`create_klipper` は接続しないので 502 に化けない）
3. `homed_axes` に xyz が揃わない → 400（G90 の絶対移動なので未ホーミングでは意味がない）
4. `clean_nozzle` が可動域外を検出 → `ValueError` → 400（送信ゼロ）

### 公開インターフェース

- `web.api.routers.nozzle_cap.NozzleCleanTestResult(message: str)`
- `POST /api/pasting/nozzle-clean/test` → 200 / 400 / 409 / 423 / 502
- UI: `data-testid="nozzle-clean-test"` のボタン（`data-requires-control`）

### タイムアウト

`CLEAN_TIMEOUT = 90.0`。パージ 0.2 uL ≈ 2 秒 + 十字往復 13 点 @5 mm/s ≈ 15 秒。
frontend の `proxy_read_timeout` は 120 秒なので同期で収まる。

## 段階 2: テスト実装（red を確認）

- `tests/web/api/routers/test_nozzle_cap.py::TestNozzleCleanTestRun` 6 件
    （未記録 400 / 座標欠落 400 / 記録済み → 502 / 使用中 409 / 実機 2 件）
- `tests/web/ui/test_pages.py` にボタン描画 1 件
- `tests/e2e/test_browser_ui.py` に「失敗してもボタンが押せる状態に戻る」1 件
    （`data-testid` の衝突チェックにも `nozzle-clean-test` を追加）

新規 5 件が 404 / 未描画で red になることを確認してから実装に入った。

## 段階 3: 機能実装

`src/web/api/routers/nozzle_cap.py` に `run_nozzle_clean_test`、テンプレートにボタン、
`nozzle_cap.js` に配線。`make format && make type && make test-no-hardware` が green（3239 passed）。

### 計画外の判断

- **実行中の無効化は `.disabled`、操作権ゲートは `inert`**。`control.js` が
    「ジョブ状態を見て `.disabled` を書くモジュールと同じ属性を使うと二重管理バグになる」と
    理由付きで分けているので、その分担に従った（`app.js` の `postTopbarCommand` と同形）
- 応答フィールド名は `message`。`postTopbarCommand` が `result.message` を読む既存の慣習に揃えた

## 段階 4: リファクタリング・自己レビュー

- 未ホーミング検査が記録と同じ規則なので `_require_all_axes_homed(klipper, detail)` に括り出した
    （文言だけ「記録してください」/「実行してください」で分ける）
- 単発の `_NOT_RECORDED` 定数を inline 化。`pasting/nozzle_clean.py` に同名で別文言の定数があり、
    名前だけ一致して中身が違う状態を残さないため
- `except ValueError` の注釈を「可動域外など」に直した（`stage.move` の limits 違反も入る）

自己レビューで拾って却下したもの:

- **未記録ならボタンを隠す**: サーバーが 400 で理由を返すので、表示条件を 2 か所に持たない
- **`clean_nozzle` 側にリトラクトを入れる**: 塗布ジョブのプライム収支が崩れる（既存契約を変えない）

## 段階 5: ドキュメント

`src/pcbasm/pasting/README.md` の依存方向に「`nozzle_clean` はジョブ以外に router からも呼ぶ」を
1 行追記。`clean_nozzle` を session 非依存にしてある理由が、この経路で実際に効いた。

## 追加要望: テスト後にノズルキャップへ戻す

ユーザーの「ノズルクリーニングの後にノズル位置に戻してください」を確認したところ、
**戻り先はノズルキャップ位置・適用範囲はテスト実行だけ**だった。

- 却下した解釈: 「クリーニング前にいた位置へ復帰」（`paste_solder` の対話的ローディングが
    使っている pos 保存 → 復帰の形）。ユーザー確認で否定された
- 却下した範囲: 塗布ジョブにも入れる。ジョブは直後に次の工程が続くので無駄な往復になる
- **`park_or_present` は使わない**。あれは末尾に `M84` を送るクリーンアップ経路で、
    脱力するとホーミングが消えて次のテストが 400 になる。`move_to_cap` + `wait_for_done` だけ送る
- キャップが未記録・可動域外でも失敗にしない。クリーニングは既に完了しているので、
    何が起きたかを応答の文言で伝えるだけにする（`_park_at_cap` が `str` を返す）
- 応答は `" / "` で連結して 1 行にする（トーストは `textContent` なので改行が潰れる）
