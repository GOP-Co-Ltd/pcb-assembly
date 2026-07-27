# 直行性テスト対話巡回フロー復帰 レビュー

## verdict: approve

仕様 1〜4 は満たしている。マージを阻害する欠陥は見つからなかった。
should-fix は 4 件（うち 1 件は pre-existing、1 件はユーザー判断が必要）。

## must-fix

なし。

## should-fix

### S1. `_orthogonality_points` の nearest 基準が座標系不整合（board 座標 vs machine 座標）

- 対象: `src/webui/jobs/posctrl.py:501-505`
- 問題: `sort_by_nearest([p.center.to3d() ...], result.stage.get_position().to2d().to3d())`
  で、並べ替え対象の `p.center` は **board 座標**、`start` は
  `stage.get_position()` = **machine 座標**。docstring の
  「pad 中心は現在位置からの nearest neighbor 順に並べる」は成立していない。
- 根拠: `_move_to(result, board_transform.apply(board_pt))` が示すとおり
  board_transform は board → machine。同ファイル `_corrected_entries`
  (:488-491) は machine 座標同士（`corrected_transform.apply(pad.center)` と
  `stage.get_position()`）で比較しており、正しい対の実例が 20 行上にある。
  configs/kurousagi の `reference_point` は (12.0, 3.8) なので、40mm 角の
  基板に対して十数 mm の系統的バイアスになる。
- 実害: 巡回順（入口 pad の選択）が最適でなくなるだけ。巡回先の集合・点数・
  ラベル形式は正しく、全 pad をちょうど 1 回訪問する。
- pre-existing: HEAD の `_run_orthogonality_test` および旧 CLI から同一コードを
  そのまま移設したもの。本 diff が新規に持ち込んだ欠陥ではないが、新規
  docstring が成立しない性質を主張している点は本 diff の責任。
- 確信度: 高（座標系の不整合）／実害の小ささも高

### S2. 周回ごとの点列再計算が実質 no-op（不要な複雑さ）

- 対象: `src/webui/jobs/posctrl.py:541`（`while` 内の `_orthogonality_points`）
- 問題: 「周ごとに現在位置から再計算」という設計意図が S1 により成立しない。
  さらに `_orthogonality_points` が呼ばれるのは周の**先頭**（四隅巡回の前）で、
  旧 CLI は四隅巡回**後**の位置を基準にしていた。結果として基準位置は
  「前周の最後の grid pad」になり、意図した「巡回開始位置基準」でもない。
- 実害: 周ごとに `Grid k/n` ↔ 物理 pad の対応が変わり得る（旧 CLI は
  Bottom-Left 固定基準なので周をまたいで安定していた）。あわせて周ごとに
  `stage.get_position()` の Klipper 往復が 1 回増える。
- 提案: ループ外で 1 回だけ組み立てる（S1 を直すなら board 座標での基準を渡す）。
  progress 分母・点数カウントの構造は変えずに済む。
- 確信度: 高

### S3. プロンプト待機中は modal dialog によりページが inert になる

- 対象: `src/webui/static/js/job_console.js:477-483`（`dialog.showModal()`）
- 問題: 本変更でジョブは稼働時間のほぼ全てを WAITING_INPUT で過ごす。
  HTML `dialog.showModal()` は dialog 外の要素を inert にするため、待機中は
  `#jc-abort`（中止）・preview overlay 切替・マシン操作パネルがクリック不能。
  UA 既定の `::backdrop` でプレビューも暗転する（`app.css` に `::backdrop`
  の上書きはない）。
- 根拠: サーバ側の abort は正しい（`_JobRuntime.abort` が
  `_pending.event.set()` で prompt 待機を起こし `JobAborted` → ABORTED）。
  壊れているのはブラウザからの到達性のみ。仕様 3 の「終了 か abort まで」の
  abort 経路が UI から実質使えない。
- 仕様 4 との関係: サーバ側は満たしている（override TTL 1s 経過後
  `PreviewService._current_override()` が None を返し `renderer(frame)` の
  生フレームへ戻る。`FrameHub` は最新 1 枚 + Condition なので待機中も
  ライブ）。ただし backdrop 暗転と 28rem の dialog がプレビュー上に載るため、
  「ベルトテンションを調整しながらカメラを見る」体験になるかは実機で要確認。
- 確信度: 中（機構は高確信、実際の見え方はブラウザ未確認）
- 判断が必要: 本変更のスコープ外（JS/CSS 未変更）。ユーザーへ確認を上げる。

### S4. `OrthogonalityMetrics` が調整前の 1 回計測のまま summary に載る

- 対象: `src/webui/jobs/posctrl.py:532`（ループ外で 1 回）／`:559-565`
- 問題: このジョブの目的はベルトテンション調整であり、ユーザーは周回中に
  機械の直行性そのものを変える。summary が報告する軸間角ずれ・スケールは
  **調整前**の値なので、「終了時点の直行性」と誤読され得る。
- pre-existing だが、旧実装は 1 周（約 1 分）で終わったため陳腐化しなかった。
  無限周回になったことで差が実質化した。
- 選択肢: (a) 現状維持（summary 文言に「調整前」を明記）／(b) 周ごとに
  再計測（`setup_board` のホーミング・基準点合わせが再走するのでコスト大）。
- 確信度: 中（仕様の解釈問題。ユーザー判断）

### S5. テストヘルパが conftest の `register_synthetic` を再実装している

- 対象: `tests/webui/jobs/test_board_ops.py:49-59`
- 問題: `catalog.register(JobDefinition(...))` を直書きしており、
  `tests/webui/jobs/conftest.py:51` の `register_synthetic`（同じ dev タブ
  合成ジョブの共通形）と重複。`JobCatalog` / `JobDefinition` の import も
  この重複のためだけに増えている。
- 根拠: skill `refactor-conventions`（共通形の再利用）。他の
  `tests/webui/jobs/test_*.py` は `register_synthetic` を使っている。
- 確信度: 高

## nit

- `src/webui/jobs/board_ops.py:1` モジュール docstring「ボード計測セットアップと
  銅箔照合ループの共有処理」が `confirm_next_point`（巡回プロンプト）を
  カバーしていない。確信度: 高
- `confirm_next_point` は呼び出し元 1 箇所（`_run_orthogonality_test`）のみ。
  CLAUDE.md「単発の用途しかないコードに抽象化は入れない」とは緊張するが、
  posctrl 側 private にすると装置なしで prompt 契約を固定できなくなる。
  計画どおりの配置で妥当と判断する。確信度: 中
- `_stream_labeled_frames` の後 `ctx.clear_frame()` を呼ばずに prompt へ入るので、
  待機開始から最大 1 秒はラベル付き静止フレームが残る。即ライブへ戻すなら
  `clear_frame()`。確信度: 高（実害はほぼない）
- `ctx.progress` の percent が周ごとに 0 へ戻り 100 に到達しない。無限ジョブでの
  進捗バーは意味を持たない（`_run_board_tour` と同じ書き方なので許容）。確信度: 高
- `tests/webui/jobs/test_board_ops.py:35` `answers: list[Any]` は全呼び出しが
  bool なので `list[bool]` で足りる（`Any` import もこのためだけ）。確信度: 高
- `tests/webui/jobs/test_posctrl.py:513` `assert len(answered) == 5` は
  `answer_next_prompt` が毎回新 id を追加するのでほぼ恒真。周回の確認には
  ならない。確信度: 高
- `tests/webui/jobs/test_posctrl.py:505` docstring に全角空白由来の不自然な
  空白「「終了」を 返す」。確信度: 高
- `job_console.js:appendLog` は `<pre>` へ無制限追記する（サーバ側は
  `deque(maxlen=500)`）。無限ジョブで DOM が単調増加するが、1 点 1 行・
  人間の操作速度なので実害なし。pre-existing。確信度: 高

## 確認した結果（問題なし）

- リソースリーク: なし。`FrameHub` は最新 1 枚 + Condition（購読者ごとの
  キューを持たない）ため、prompt 待機で `camera.capture()` を呼ばなくても
  フレームは蓄積しない。log は `deque(maxlen=500)`、preview override と
  progress は 1 スロット。artifacts 生成なし。
- abort: `ctx.checkpoint()`（ループ先頭・`_stream_labeled_frames` 内）と
  prompt 待機中 abort の双方でサーバ側は ABORTED になる。
  `JobManager.shutdown` も `request_abort` → join で正しく畳める。
- 終了経路: JS の confirm は `event.submitter?.id !== "jc-prompt-no"` で bool を
  返し、`_coerce_answer` の confirm 分岐がそれを受ける → 「終了」= False =
  正常終了。JS / router / CSS の変更は不要（`webui-thin-wrapper` 準拠）。
- `total_points` を confirm の前に加算する点: 加算時点で移動 + プレビューは
  完了しているので「訪問済み点数」として整合。「終了」を押した点も 1 点として
  数える。仕様どおり。
- 周回カウンタ: `for ... else: cycle += 1` により、「終了」で抜けた周が
  summary の「M 周目」になる。仕様どおり。1 周完走直後に「終了」を押した場合は
  次周の 1 点目扱いで M+1 になり、これも仕様に沿う。
- progress stage の文言変更（「四隅巡回」「グリッド巡回」→「巡回 N 周目」）に
  依存する JS / router / テストは無い（`commandReady` の stages 指定に
  これらは含まれない）。
- 成果物汚染（`</content>` 等）なし。4 ファイルすべて末尾正常。

## 検証結果

- make format: pass（pre-commit 全 hook Passed、ファイル md5 変化なし）
- make type: pass（pyright 0 errors）
- make test-no-hardware: pass（1608 passed / 88 deselected / 52.89s）
- 実機テスト（`-m hardware`）: 未実行（ユーザー実行）
