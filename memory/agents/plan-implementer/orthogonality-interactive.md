# 直行性テストを対話巡回フローへ戻す

## 実装内容

- `src/webui/jobs/board_ops.py`: `confirm_next_point(ctx, label) -> bool` を新設
  （`setup_board` の直後に配置）。`PromptSpec(kind="confirm", default=True,
  true_label="次へ", false_label="終了")` の結果を `bool(...)` で返すだけ。
  `JobAborted` は捕捉せず伝播（docstring の Raises に明記）
- `src/webui/jobs/posctrl.py`:
  - `_orthogonality_points(result) -> list[tuple[str, Point2d]]` を新設（点列構築
    ヘルパ。四隅 + TOP 層 pad 中心の nearest 順）
  - `_run_orthogonality_test` を対話巡回（無限周回・「終了」で正常終了）へ書き換え
- `_run_board_tour` は未変更

## 計画外の判断ログ

- **点列ヘルパの名前**: `_orthogonality_points`。`_board_corners` / `_corrected_entries`
  と同じ「巡回先を組み立てて返す private ヘルパ」の命名列に合わせた
- **nearest 順を計算する基準位置**: 周回の**先頭**（四隅へ入る前）の現在位置から
  計算する。仕様「周ごとに現在位置から再計算」を、点列を 1 回で組み立てる形
  （`len(points)` で progress・summary の巡回点数を出す）と両立させるため。
  旧実装は四隅を回り終えた位置を基準にしていたので、この点だけ厳密には挙動が違う。
  ヘルパを 2 分割して四隅巡回後に grid を組み立て直せば旧挙動に一致するが、
  progress 分母と点数カウントが複雑化するので採らなかった
- **ループ構造**: `while not finished:` + 内側 `for ... else: cycle += 1` にして
  return を loop の外に 1 つだけ置いた。`while True` 内 return だと pyright の
  到達解析が読みにくくなるため。`assert` / 余分な `raise` は追加していない
- **周回カウンタ**: for が完走したときだけ `cycle += 1`。「終了」で抜けた周が
  summary の「N 周目で終了」になる
- **docstring の改行**: docformatter (`--wrap-descriptions=72`) が日本語行を
  連結して行内に不自然な空白を作るため、各段落を 1 物理行（<=68 文字）に収めた

## 他 implementer への IF 変更通知

なし（計画のシグネチャどおり）。

## 既知の制約・残課題

- 巡回本体（移動・プレビュー・周回）は `setup_board` が実 Klipper を要求するため
  `test-no-hardware` では検証不能。spec-test-author は prompt spec と True/False の
  意味を `test_board_ops.py::TestConfirmNextPoint`（実 JobManager 経由の合成ジョブ）
  へ、通しを `@mark_hardware` へ分担している
- 「終了」を押さず放置すると `ctx.prompt` で無期限待機する（abort で抜ける）。
  旧 CLI の `while True` と同じ性質で、タイムアウトは意図的に入れていない

## 検証結果

- make format: pass
- make type: pass（0 errors）
- make test-no-hardware: pass（1608 passed / 88 deselected）
