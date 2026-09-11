# 通知音の発火条件を再設計する（audio-notification-coverage）

## 背景

`success` / `failure` / `prompt` の 3 音があるが、実際に鳴るのは 2 ジョブのみ。
「オペレータの注意を要する状態」がジョブ定義ごとのオプトインになっているのが根本原因。
加えて `ctx.next_command(timeout=None)` のコマンド待ちは prompt ですらないため、
手動ペーストローディング・吐出量キャリブのメニュー待ちは仕組み上鳴らせなかった。

## 段階 1: 計画（確定した方針）

ユーザー確認の結果:

| 音 | 新しい発火条件 |
| --- | --- |
| `prompt` | オペレータ待ちに入るたび**常時**。`PromptSpec.notify` は廃止 |
| `success` | `uses_machine=True` かつ SUCCEEDED |
| `failure` | `uses_machine=True` かつ FAILED（ABORTED は無音のまま） |

`notify_on_completion` は音声から切り離し、ブラウザ完了通知専用フラグとして現状維持。
→ ジョブ定義側の変更はゼロ。差分は manager / context / 待機側の呼び出しに集中する。

### 公開インターフェース

- `JobContext.notify_operator() -> None` を新設（コマンド待ちなど prompt 以外の
  オペレータ待ちで鳴らす）。`JobContextBridge` Protocol にも追加
- `PromptSpec.notify` 削除、`prompt_positive_number(..., notify=)` 削除

### 呼び出し追加（blocking な `next_command` 待ち 3 箇所）

- `common.run_loading_loop` — 手動ペーストローディング
- `dispense_calibration._calibration_menu_loop` — メニュー待ち（初回＋各サブキャリブ復帰時）
- `dev._run_job_demo` — コマンド待ち

`posctrl.py:184` と `common.drain_commands` は `timeout=0` のポーリングなので対象外。

## 段階 2: テスト実装

- `test_manager.py` の `TestAudioCompletionNotification` を `uses_machine` 基準へ書き換え。
  `notify_on_completion=True` × `uses_machine=False` が**鳴らない**ことを足して、
  ブラウザ通知フラグとの分離をピン
- `TestAudioPromptNotification` → `TestAudioOperatorNotification`。全 prompt が毎回鳴ること、
  `ctx.notify_operator()` が同じ音を鳴らすことを検証
- `test_dev.py` に job_demo のコマンド待ち通知テストを追加。**prompt を出さないオペレータ待ち**を
  Klipper 不要の実ジョブで通しで検証できる唯一の経路（loading / キャリブメニューは
  Klipper 必須で非実機テストから到達できない）

## 段階 4: 自己レビュー

- `_JobRuntime` の注入 callable `self._notify_operator` と bridge メソッド
  `notify_operator()` が同名で紛らわしいが、`self._publish` / `publish_status` と同じ既存パターン。
  Protocol を満たすには明示メソッドが要るため現状維持
- 却下: `next_command` 自体を通知点にする案。ローディングはボタンを押すたびにループを
  回るため、押下のたびに鳴って耐えられない。段階入口で 1 回だけ鳴らす明示呼び出しを採った
- キャリブメニューはサブキャリブ末尾の choice prompt 直後にメニュー復帰通知が続き、音が 2 回
  連続しうる。待ちの種類が変わる（入力 → メニュー操作）ので意味的には正しく、許容した
- 残ギャップ: `run_loading_loop` と `_calibration_menu_loop` の通知呼び出し自体は
  非実機テストで到達できず、実機テスト頼み。`Klipper` / `PasteApplicator` は自前 ABC ではなく
  具象クラスのため testing-strategy 上 fake を作れない

## code-reviewer の指摘と対応（verdict: request-changes）

対応した:

- **must-fix**: `reference_point_setup` のジョグ待機（`posctrl.py`）が未通知。除外基準を
  「`timeout=0` のポーリングか」という*実装機構*で引いていたのが誤りで、正しくは
  「オペレータ待ちか」という*意味*。`drain_commands` の除外は待ちではないので妥当だが、
  こちらは待ちそのものだった。`while True:` の直前に 1 行追加
- **should-fix**: abort 済みでも `notify_operator()` が鳴っていた。死んだジョブの前へ作業者を
  呼び戻すことになる。`_JobRuntime.notify_operator` で `abort_event` を見て黙る。
  `checkpoint()` にしなかったのは、通知は副作用であって制御点ではなく、
  `JobContext.notify_operator` が `JobAborted` を投げる契約にしたくないため
- **should-fix**: 「`Klipper` / `PasteApplicator` は具象クラスだから fake 不可」は**私の誤り**。
  `tests/helpers.py` に `FakeKlipper(Klipper)` が既にあり、実 `XYZStage` / 実
  `PasteApplicator` と組める。`run_loading_loop` の finish 経路は klipper に触れないので、
  合成ジョブから呼んで `{"type":"finish"}` を送るだけの非実機テストが書けた
  （`TestLoadingLoopNotification`）。5 ジョブ共通の入口が固定できた
- **nit**: 完了音の対象集合（`uses_machine`）を `test_catalog.py` で仕様として固定
- **nit**: docformatter の折返しで読みにくかった docstring 2 件を言い換え

対応しなかった:

- **nit「`register_gated(notify_on_completion=)` が未使用」は誤り**。
  `tests/web/api/routers/test_jobs.py` が使っている。一度消して pyright で検出、復帰
- **nit「入力待ち音だけ古い設定を見る」も誤り**。`manager.py` の `machine` は
  `_operator_notifier(machine)` と `JobContext(machine=machine)` へ同一オブジェクトが渡る。
  完了音の `context.machine.audio` と同じジョブ開始時スナップショット
- **should-fix「`orthogonality_test` / `board_tour` が巡回 1 点ごとに鳴る」**: 方針 1（常時）は
  ユーザーの明示決定。実装は準拠しているので変更せず、事実としてユーザーへ報告する
- **nit「`dataset_finalize` / `paste_volume_refit` の失敗が無音」**: 方針 3 どおり。
  ユーザーは failure 音を「uses_machine のみ」と明示的に選んだ（無条件案を却下）
