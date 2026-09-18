# 流量キャリブレーションで dispense_accel を変更しない

## 要件（ユーザー）

吐出量キャリブレーションの ① `rotations_per_ul` 検証ループで、採用時に
`dispense_accel` まで連動変更している。吐出加速度は machine.toml の設定値のままとし、
流量キャリブレーションは `rotations_per_ul` だけを変更する。

## 段階 1: 計画

### 現状

`RotationsPerUlRound.evaluate` が `previous_dispense_accel` を受け取り、
`FlowCalibration.rescaled_dispense_accel`（回転加速度 rev/sec² を保存する再スケール）で
新 `dispense_accel` を算出。`_calibrate_rotations_per_ul` が採用時に
`paste_dispenser.rotations_per_ul` と `paste_dispenser.dispense_accel` の両方を
machine.toml へ書き、`FlowCalibrationProcedure.adopt` が `_dispense_accel` も更新していた。

なお `PasteSession.make_applicator` は `dispense_accel` を受け取らず、常に
machine 設定の値を使う。`procedure._dispense_accel` は machine.toml 反映と
次ラウンドの再スケール入力のためだけの帳簿であり、実際の G-code には効いていない。

### 公開インターフェースの変更

- `FlowCalibration.rescaled_dispense_accel` を削除（唯一の呼び出し元が消えるため）
- `RotationsPerUlRound`: `dispense_accel` フィールドを削除、
    `evaluate(..., previous_dispense_accel=...)` 引数を削除
- `FlowCalibrationProcedure.dispense_accel` プロパティを削除
- `_DispenseCalibrationResults.dispense_accel` を削除（summary からも外れる）

温存するもの:

- `FlowCalibration.dispense_accel_for` / `MassFlowEstimate.dispense_accel` は
    ローディング画面（`/pasting/loading`）の rev→μL 換算ツールで、ユーザーが
    明示的に「反映」を押したときだけ書き込む別機能。今回の「勝手に変わる」問題では
    ないので触らない

### テスト観点

- `RotationsPerUlRound.evaluate` が accel 入力なしで `computed` / `rotations_used` を出す
- `adopt` 後も G-code の吐出加速度（μL/sec² 換算）が machine 設定値のまま
- 収束判定・非正値バリデーションは既存どおり

### リスク

- なし（`dispense_accel` は元々 applicator へ渡っておらず、削除で実走の挙動は変わらない。
    変わるのは machine.toml へ書く値と summary のみ）

## 段階 2-3: テスト → 実装

計画どおり。`make format` / `make type` / `make test-no-hardware`（3343 passed）green。

worktree 特有の注意: `uv run` が worktree 直下に新しい `.venv` を作るが、既定では
`include-system-site-packages = false` になり `picamera2` 等のシステム提供パッケージを
import できない。`.venv/pyvenv.cfg` を `true` に直せば本体チェックアウトと同条件になる。

## 段階 4: 自己レビュー + code-reviewer

自己レビューで test ヘルパーの重複（`_dispense_distances_mm` と accel 抽出）を
`_dispense_moves` / `_move_param` に整理した。

`code-reviewer` は approve。対応した指摘:

- nit: テスト名の "values"（複数形）→ `test_adopt_updates_rotations_per_ul_...` に改名
- nit: `_move_param` を呼び出し元より前へ移動
- 追加した G-code アサーションの過大な主張をコメントから外した

対応しなかった指摘と理由:

- should-fix「実害（machine.toml へ accel を書く）を pin するテストがない」:
    `_calibrate_rotations_per_ul` は prompt / `run_loading_loop` / 実機 procedure 駆動で、
    テスト化にはジョブ層をスクリプト化した JobContext harness が要る。
    AGENTS.md のモック最小化方針とのトレードオフが割に合わないため見送った。
    再発防止は「`RotationsPerUlRound` が accel 値を一切持たない」という構造で担保する
- nit: `memory/agents/implementation-planner/pasting-mr4-flowcalib.md` に旧 IF が残る:
    過去 MR の作業記録であり、現状コードの正典ではないので書き換えない

## ユーザーへの申し送り

- ローディング画面（`/pasting/loading`）の「すべて反映」は今も `dispense_accel` を
    `rotations_per_ul` と同時に書く。あちらは rev→μL 換算ツールでユーザーが明示的に
    押す操作なので今回は温存した。ここも止めるかは要判断
- 物理的な意味: `dispense_accel` [μL/sec²] を固定すると、モーター角加速度
    （= `dispense_accel × rotations_per_ul`）は rotations_per_ul に比例して変わる
    （旧実装は角加速度のほうを保存していた）。要求どおりだが実機確認が要る
