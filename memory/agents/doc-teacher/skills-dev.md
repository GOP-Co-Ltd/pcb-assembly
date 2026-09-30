# 開発系 skill 5 本 理解度テスト

## 対象
- ファイル: `.claude/skills/{testing-strategy,hardware-test,refactor-conventions,webui-e2e,webui-thin-wrapper}/SKILL.md`（同名の `.agents/skills/<name>/SKILL.md` へ同一改稿を反映。Codex 向けの表記差は保つ）
- 生徒に読ませるのは `.claude` 側だけ
- 読者と用途: コーディングエージェントが、テストの書き方・実機テストの扱い・実装規約・WebUI の E2E 検証と薄いラッパー方針を誤りなく実践する

## 評価用問題
### Q1 `cv2.imread` を呼ぶ関数のテストで、`cv2.imread` を `mocker.patch` で差し替えてよいか。理由も答えよ。
- 要点: 不可（3rd-party 表面のモック禁止）
- 要点: 理由は仮定のミラーになり upstream の挙動変更を検出できないから（実 PNG を実 OpenCV に通す）

### Q2 新しいモジュールが自前 HAL ABC の `Camera` に依存している。実機なしで常時走るテストでは `Camera` の代わりに何を使うか。
- 要点: `tests/helpers.py` の `FakeCamera`（自前 HAL ABC の fake は許可されている）
- 要点: 具象が無い ABC なら `tests/helpers.py` に test 用 Impl を置き、`mocker.Mock` より先に検討する（どちらかの趣旨があれば可、FakeCamera は必須）

### Q3 `scripts/` に追加したシェルスクリプトを検証するテストは、どのディレクトリに置くか。理由も答えよ。
- 要点: `tests/` 直下
- 要点: `src/` 側にミラー元が無いリポジトリ資産のテストだから

### Q4 frontend が中継する MJPEG ストリームを検証したい。`tests/web/ui/` と `tests/e2e/` のどちらに書くか。理由も答えよ。
- 要点: `tests/e2e/`
- 要点: `tests/web/ui/` が使う `ASGITransport` はレスポンスを最後までバッファするので、終端しない MJPEG を読むとハングする

### Q5 テストに新しいマーカー `slow` を付けたい。マーカーを付ける前に何をする必要があるか。
- 要点: `pyproject.toml` の `[tool.pytest.ini_options]` の `markers` に登録する
- （R1 で削除: 「`--strict-markers` で未登録はエラー」は問われていない理由なので要点から外した）

### Q6 Claude として `tests/pcbasm/hal/` のテストだけを走らせたい。実行するコマンドを答えよ。
- 要点: `uv run pytest tests/pcbasm/hal/ -m "not hardware"`（`-m "not hardware"` が必須）
- 要点: `-m hardware` や `make test` は使わない（実機が動く）※コマンドが正しければこの点は明記がなくても可

### Q7 mDNS 探索の E2E テストで、探索結果の件数が 1 件であることを assert してよいか。どう書くべきか。
- 要点: 総件数で assert しない（実 LAN の他の広告を拾い得るため）
- 要点: 期待した machine_id が現れることを assert する

### Q8 ブラウザ E2E で設定の「保存」ボタンを押すテストを書いたが、クリックが効かない。テストに何を足すか。理由も答えよ。
- 要点: 変更操作の前に `acquire_control(page)` を呼んで操作権を取る
- 要点: UI は fail-closed で開いた直後は viewer、`data-requires-control` の要素に `inert` が付いてクリックが届かない

### Q9 ボタンが閲覧者には操作不可であることを検証したい。Playwright の `is_enabled()` で判定してよいか。
- 要点: 不可（`is_enabled()` は `inert` を検出しない）
- 要点: `evaluate` で `el.hasAttribute('inert')` を読む

### Q10 backend 2 台を frontend に登録したマシン切替の E2E を書きたい。どう組むか。
- 要点: 既存 fixture には無いので、テスト内で `start_app` を使って実 uvicorn を起動する
- 要点: backend の machine_id は `make_api_settings(..., hostname=...)` で分ける（frontend は `make_ui_settings`）
- 要点: 起動したサーバーは try-finally で `stop()` する

### Q11 Claude として `make api-fake` を起動し、JSON API を一度だけ手で叩いて確認したい。起動・待機・後始末をどうするか。
- 要点: foreground で起動しない（120s で殺される）。`run_in_background: true` の Bash で起動する
- 要点: 別の Bash で `curl --retry ... --retry-connrefused` で readiness を待って叩く（foreground の sleep は使わない）
- 要点: 確認後に起動した background プロセスを止める

### Q12 設定フォームの値が正の整数かどうかの検証を JS に追加してよいか。JS に残してよい検証は何か。
- 要点: 追加しない。正値・整数などドメイン制約は Python 側に一元化し、サーバの 400 を表示する
- 要点: JS に残すのは空欄なら送らない・`Number.isFinite` でのパース可否・未接続なら送らない（1 つ以上挙がれば可）

### Q13 入力文字列を `Position` にパースする関数で、不正な入力のとき関数はどう振る舞うべきか。
- 要点: `None` を返す（戻り値型 `Position | None`）
- 要点: 呼び出し側に try-catch を強いない

### Q14 WebUI 用に新しいバリデーションを追加する。バリデーション本体はどこに置き、HTTP エラーへの変換はどこで行うか。
- 要点: 本体は pcbasm（`src/pcbasm/`）に置く（R1 で「None 返却型」を要点から外した。問いが戻り値の形を尋ねておらず、None 返却は Q13 で測る）
- 要点: router 側で `HTTPException` に変換する

## 保留問題
### H1 実機テストに `@pytest.mark.hardware` と直接書いてよいか。
- 要点: 不可。`tests.helpers` の `mark_hardware` を import して付ける

### H2 テストで内部属性 `_points` の値を確認したい。属性を public にしてよいか。
- 要点: 不可。「テストから参照したい」は public 化の理由にならない
- 要点: 公開 API 経由で振る舞いをテストする

### H3 描画用に値から表示色を補間する処理と、階層 override の解決処理がある。JS に書いてよいのはどちらか。理由も答えよ。
- 要点: 色補間は JS 可、override 解決は不可（pcbasm の resolve 系を使う）
- 要点: 判定基準は「サーバの真実と一致すべき結果」か「ピクセル/色のための変換」か

## ラウンド記録
### R0 執筆（行数 .claude 側: ts 132→132, hw 81→86, rc 131→131, e2e 185→188, tw 66→66 / 計 595→603）
- 事実確認で直した矛盾:
  - testing-strategy: レイアウト例の `vision/detect.py` `geometry/plane.py` は実在しない → `detection.py` / `polygon.py`
  - testing-strategy: 登録済みマーカーは `hardware`/`e2e`/`browser`。`api_contract` は未登録で `test_api_contract.py` も未作成 → 登録手順を明記
  - hardware-test: `skip_if_no_alsa_audio` / `skip_if_no_mdns` が表に無い。例の `fake_camera` fixture は存在しない → `FakeCamera(images)`。遅延評価の具体（`_skip_unless_available`）を明記。直接 pytest 時の `-m "not hardware"` を追記（CLAUDE.md の規則、hook で強制）
  - webui-e2e: `live_ui_two` fixture は削除済み（404a3a9）→ `start_app` + `make_api_settings(hostname=)` + `make_ui_settings` で組む旨に差し替え。`make test-e2e` は `--timeout=180` 付き
  - webui-thin-wrapper: layout のテーブル名を `TAB_LABELS` / `FEATURE_LABELS` に具体化
  - .agents/refactor-conventions: モック方針が testing-strategy と矛盾（HW デバイス等のモックを許可）→ .claude 版の要約に同期
  - .agents/webui-e2e: 実在しない memory `webui-implementation` への参照を削除

### R1（.claude 側 計 603 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1-Q4 | 正解 | - | - |
| Q5 | 不正解→要点修正後 正解 | 問題不良 | 問われていない理由（strict-markers）を要点に入れていた。要点から削除 |
| Q6-Q13 | 正解 | - | - |
| Q14 | 不正解→要点修正後 正解 | 問題不良 | 問いが尋ねていない「None 返却型」を要点に入れていた。要点から削除（Q13 と重複） |
- 元の要点では 12/14。要点修正後は 14/14。文書は変えていない。生徒の「読みにくかった箇所」はなし
- 次: 保留問題 H1-H3 で汎化を確認する

### 汎化確認（保留問題、603 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1 | 正解 | - | - |
| H2 | 正解 | - | - |
| H3 | 正解 | - | - |
- 3/3 正解。ループ完了。読みにくかった箇所はなし
