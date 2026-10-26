# 実機受け入れ試験

[ドキュメント一覧](../README.md) · [保守ガイド](maintenance.md) · [開発の手引き](develop/index.md)

実機での確認はこの文書にだけ書く。pytest は実機に触れない（Klipper への接続と systemctl の呼び出しは、テスト中は必ず失敗する）。
送信する G-code の列は `tests/golden/` の golden で固定し、ここでは golden で見えないもの（機構の動き、カメラ、音、画面）を確かめる。

001 は `usaremino001`、002 は `usaremino002` を指す。

## merge 前の手順

### 1. 001 でブランチを確かめる

本番の config と data のまま、ブランチのコードで 001 を動かす。
確認の操作（教示、校正、Apply、即時書込）は本番の設定を書き換えるので、先に退避し、最後に戻す。

1. 設定と校正を退避し、本番のサービスを止める

    ```sh
    backup=~/acceptance-backup/$(date +%Y%m%d-%H%M%S)
    mkdir -p "$backup"
    cp -a ~/pcb-assembly/config ~/pcb-assembly/data/paste-volume-calibrations "$backup"/
    sudo systemctl stop pcbasm-ui pcbasm-api
    ```

2. worktree でブランチに切り替え、依存をそろえる

    ```sh
    cd ~/pcb-assembly/.claude/worktrees/<worktree>
    git switch <ブランチ>
    uv sync --locked --inexact  # worktree に .venv が無ければ make setup
    ```

3. 別々のターミナルで、config と data を本番に向けて起動する

    ```sh
    export PCBASM_CONFIG_DIR=$HOME/pcb-assembly/config PCBASM_DATA_DIR=$HOME/pcb-assembly/data
    make api  # もう一方のターミナルでも export してから make ui
    ```

4. 下の対応表から、変えた領域の項目を実施する。PR 本文に、実施した項目と対象機、ジャーナルの前後 diff を書く

5. 両方を Ctrl-C で止め、退避したときからの設定の変化を見る

    ```sh
    diff -ru "$backup/config" ~/pcb-assembly/config
    diff -ru "$backup/paste-volume-calibrations" ~/pcb-assembly/data/paste-volume-calibrations
    ```

    差分のうち、教示し直すと決めて取った値（キャップ位置など）でないものは、`$backup` のファイルから戻す。
    全部戻すなら `cp -a "$backup/config/." ~/pcb-assembly/config/` を実行する

6. 本番のサービスを戻し、本番の WebUI で本番の PCB を選び直す

    ```sh
    sudo systemctl start pcbasm-api pcbasm-ui
    ```

- 確認中にアップロード・選択した PCB は worktree の `uploads/` にあり、本番の選択（`data/webui/webui_state.json`）はそれを指したまま残る。worktree を消すと選択が外れるので、手順 6 で選び直す
- 確認中のファイルブラウザは worktree と `/media`、`/mnt` しか開けず、本番の `~/pcb-assembly/uploads` は開けない。本番の PCB を使うなら、確認の前に本番の WebUI で選んでおく
- この起動では active な unit が無いので、再起動（復旧）は Klipper だけを再起動し、WebUI のサービスは再起動しない。サービスの再起動まで含めた A14 は、merge 後に本番の unit で行う
- ジャーナル（`data/webui/journal/`）は本番と共有する。同じジョブを前の版とブランチで 1 回ずつ流し、2 つのファイルを diff する。前の版がジャーナルを持たない間は、golden を基準にする
- 運用データを書き換える PR（設定の移行、基板 override の付け替え、校正 JSON の形式変更）では、確認で本番のファイルが書き換わる。不合格なら、次の手動ロールバックで元に戻す

### 2. 002 は保留する

001 の受け入れが通るまで、002 は更新しない。
運用データを書き換える PR は、merge の前に両機を直前の版まで更新し、両機の `/api/update/status` の head を PR 本文に記録する。merge 後に 002 で A19 を行う。

### 3. 手動ロールバック

[保守ガイドの手動ロールバック](maintenance.md#%E6%89%8B%E5%8B%95%E3%83%AD%E3%83%BC%E3%83%AB%E3%83%90%E3%83%83%E3%82%AF) の手順で、前の head から復旧ブランチを作って戻す。
設定は `config/history/` の世代から戻す。
設定履歴を持たない古い版へ戻すときは、「1. 001 でブランチを確かめる」の手順 1 で退避したファイルから戻す。

## 変えた領域と、実施する項目

| 変えた領域                                            | 実施する項目                                 |
| ----------------------------------------------------- | -------------------------------------------- |
| `pcbasm.hal`、`pcbasm.machine`                        | A0, A1, A2, A8, A10, A14, A20                |
| `pcbasm.gcode`、`pcbasm.geometry`                     | A1, A5, A8                                   |
| `pcbasm.align`、`pcbasm.vision`                       | A3, A4, A5, A8                               |
| `pcbasm.board`、`pcbasm.kicad`                        | A8, A17                                      |
| `pcbasm.pasting`                                      | A6, A7, A8, A9, A15, A16, A21                |
| 描画（`pcbasm.plotting` と `render_*`）               | A7, A9, A17                                  |
| `pcbasm.config`、`web.api.machine_config`、設定の移行 | A10, A13, A18, A19, A22 と、全項目の起動確認 |
| `web.api`、`web.ui`                                   | A1, A8, A12, A17, A18 の画面操作             |
| `web.selfupdate`、`scripts/`、unit                    | A11                                          |
| `tests/golden` の更新                                 | A8 と、差分が出たジョブ・route の項目        |

## 項目

| 項目 | 内容                             | 対象機                                 |
| ---- | -------------------------------- | -------------------------------------- |
| A0   | 装置診断                         | 001 / 002                              |
| A1   | 装置操作                         | 001                                    |
| A2   | ノズルキャップとクリーニング     | 001                                    |
| A3   | 基準点の教示                     | 001                                    |
| A4   | カメラ校正                       | 001                                    |
| A5   | 基板の巡回と直交性               | 001                                    |
| A6   | ローディング                     | 001                                    |
| A7   | 高さ平面                         | 001                                    |
| A8   | はんだ塗布の通し                 | 001（データを書き換える PR は 002 も） |
| A9   | オフセットと吐出量・塗布量の校正 | 001                                    |
| A10  | 通知音、プレビュー、設定の保存   | 001                                    |
| A11  | 自己更新                         | 001 / 002                              |
| A12  | 複数機体                         | 001 / 002                              |
| A13  | 設定エラーからの復旧             | 001                                    |
| A14  | 移動中の緊急停止と復旧           | 001                                    |
| A15  | クリーニング分岐の強制           | 001                                    |
| A16  | 校正の残り                       | 001                                    |
| A17  | 基板とプレビュー                 | 001                                    |
| A18  | Apply と即時書込                 | 001                                    |
| A19  | 002 での確認                     | 002                                    |
| A20  | 電源の入れ直し                   | 001                                    |
| A21  | 所要時間の予測と実測             | 001                                    |
| A22  | 機体の機能と [solder] への移行   | 001（002 は A19）                      |

### A0 装置診断

- 画面: 装置診断（`machine_check`）。機体タブの「状態と診断」にある
- 操作: 開始を押し、ログに並ぶ項目を読む。装置は動かない
- 合格条件: 全項目が ok でジョブが終わる。Klipper が ready で、printer.cfg の必須 section がそろい、カメラの校正に焦点 Z があって解像度が校正と一致し、`[audio]` の出力デバイスが ALSA に見える

### A1 装置操作

- 画面: マシン操作パネル、Klipper ステータス（`klipper_status`）
- 操作: 全軸ホーミング → X を +0.1 mm、-0.1 mm ジョグ → 可動域外への絶対移動 → Relax → ジョグ → G-code 送信（`M400`）
- 合格条件: ホーミング後の homed axes が `xyz`。ジョグの前後で位置表示が 0.01 mm 以内で一致する。可動域外の移動は 400 で、ステージは動かない。Relax の後のジョグは拒否される。G-code 送信が成功し、Stage Limits に各軸の min < max が出る

### A2 ノズルキャップとクリーニング

- 画面: ノズル位置の設定（`nozzle_cap`）、マシン操作パネルの「キャップ位置へ」
- 操作: ホーミング後にキャップ位置とクリーニング位置を記録する → クリーニングのテストを 2 回続けて押す → もう一度押して、ホーミング中に「中止」を押す → 塗布系のジョブを 1 つ終える → 全軸ホーミングする → 設定ページでクリーニング位置 X を可動域の max より大きくしてテストを押し、元の値に戻す
- 合格条件: 記録した位置が `config/machine.toml` の `[solder.nozzle.cap]` と `[solder.nozzle.clean]` に入る。テストはジョブとして走り、同じページにログが出る。こすりの後に TRAVEL_Z へ戻り、終了時にキャップへ駐機して脱力する。2 回目は脱力しているのでホーミングしてからクリーニングする。ホーミング中の中止ではパージせずにキャップへ駐機して「中止」で終わる。ジョブの終了時にキャップへ駐機する。可動域外の位置でのテストは「クリーニング位置が可動域外です」で失敗し、クリーニング位置へは動かず、パージも出ない（終了時の駐機だけ行う。脱力したまま押すと、キャップへの移動を Klipper が断り「タスク終了時の退避に失敗」が出る）

### A3 基準点の教示

- 画面: `reference_point_setup`
- 操作: ジョグ → record。もう一度開始して quit
- 合格条件: record で現在位置が設定に反映され（Apply は不要）、summary の x / y と設定ページの値が一致する。基準円がオーバーレイと合う。quit は中止になり、設定は変わらない

### A4 カメラ校正

- 画面: `camera_calibration`
- 前提: 1 マス 1.5 mm のチェッカーボードを視野に置く
- 合格条件: 結果の JSON に z_position が記録され、新しいファイル名で保存される（旧ファイルは残る）。Apply の後、再起動せずに次のジョブで新しい校正が使われる

### A5 基板の巡回と直交性

- 画面: `board_tour`、`orthogonality_test`
- 前提: 基準点マーカーが視野に入るように基板（例: `tests/data/fill_coverage`）を置き、その PCB を選ぶ
- 操作: board_tour を通す。orthogonality_test は各点の確認で「次へ」を押し、途中で「終了」を押す
- 合格条件: board_tour が成功し、summary に照合の残差が出る。orthogonality_test は確認を待つ間ずっと十字線・ROI・巡回先ラベルが出続け、「終了」で正常終了して summary に軸間角・巡回点数・周回数が出る。残差と軸間角が従来値と同程度である（前後の値を PR 本文に記録する）

### A6 ローディング

- 画面: `loading`
- 操作: 位置 X を指定して開始 → 押出 → 回転押出（引き戻しあり・なし）→ 負の量で押出 → ジョグ → 終了
- 合格条件: 開始時に全軸ホーミングしてから指定の X へ移動する。summary に押出合計 [uL] と回転合計 [rev]（押出から引き戻しを引いた純増）が出る。負の量は押し出さずに理由をログに出し、ループは続く。ループ内でジョグが効く

### A7 高さ平面

- 画面: `height_plane`
- 合格条件: プローブが通しで動き、計測予定点とヒートマップの画像が成果物に出る

### A8 はんだ塗布の通し

- 画面: `paste_solder`
- 前提: テスト基板（例: `tests/data/led_blinker`）をセットし、その PCB を選ぶ
- 操作: probe → 領域照合 → pad の精密照合 → 初回パージの撮影確認 → 塗布 → 駐機まで通す
- 合格条件: 塗布がパッドに乗り、キャップへ駐機して終わる。前の版とのジャーナルの diff に、意図しない差が無い

### A9 オフセットと吐出量・塗布量の校正

- 画面: `toolhead_offset`、`dispense_calibration`（①）、`paste_volume_calibration`（小さい格子）
- 合格条件: それぞれ値が保存され、Apply の後に設定ページの値と一致する

### A10 通知音、プレビュー、設定の保存

- 画面: 通知音（`audio`）、カメラプレビュー（`camera_preview`）、マシン設定
- 合格条件: 選んだデバイスで音が鳴る。プレビューが動く。設定を保存した後も `machine.toml` のコメントが残り、`config/history/` にスナップショットができる

### A11 自己更新

- 画面: ソフトウェア更新（`/update`）、機体一覧
- 合格条件: 001 と 002 の両方で WebUI から更新できる。版のずれた機体が「更新専用」で見える。unit drift の警告が誤って出ない

### A12 複数機体

- 画面: 機体一覧、機体の切替
- 合格条件: 機体一覧から切り替えられる。操作権が機体ごとに独立している。更新専用の機体でも緊急停止できる

### A13 設定エラーからの復旧

- 操作: `machine.toml` をわざと壊して backend を起動する（値の誤り、例: `lift_height = 0`。書式の誤り、例: 閉じていない文字列）
- 合格条件: backend が起動し、機体一覧に載る。全ページに設定エラーの理由が出て、ジョブの開始は断られる。緊急停止が効く。値の誤りは設定画面から、書式の誤りは設定履歴の復元から直せ、バックアップも取れる。直すと再起動なしで理由の表示が消える

### A14 移動中の緊急停止と復旧

- 操作: 長い移動（例: 可動域の端から端への絶対移動）の途中で緊急停止を押す → 再起動（復旧）を押す
- 合格条件: 緊急停止ですぐ止まり、実行中のジョブは中止になる。再起動（復旧）の後、ssh などの手当てなしに画面が戻り、ホーミングから操作を再開できる

### A15 クリーニング分岐の強制

- 操作: 初回パージ量（`initial_purge_ul`）を検出の下限より小さくして `paste_solder` を開始する
- 合格条件: パージが写らず、ノズルクリーニング → やり直しの経路を通る。終わったら初回パージ量を元に戻す

### A16 校正の残り

- 画面: `dispense_calibration`（②③）、`paste_volume_refit`、`paste_dataset_finalize`
- 合格条件: ②③が値を保存する。refit が保存済みのデータセットから校正を作り直す。収集を途中で止めてできた `.incomplete` を、finalize で確定できる

### A17 基板とプレビュー

- 画面: `paste_solder` の PCB 選択と pad editor
- 操作: PCB をアップロードし、USB メモリ（`/media`）の PCB も開く。pad editor で export → import する。route / fill-path / copper / initial-purge のプレビューを出す。pad を変えた基板を同じ名前で再アップロードする
- 合格条件: どの操作もエラーにならない。再アップロードの後、新しい pad が表に出て、塗布できる

### A18 Apply と即時書込

- 対象: solder.dispenser.rotations_per_ul、solder.dispenser.max_dispense_rate、solder.motion.max_fill_speed、solder.initial_purge_ul、solder.nozzle.offset、reference_point、camera.calibration_file、alignment.canny_low
- 合格条件: どれも書込が成功し、設定ページの値と一致する

### A19 002 での確認

- 対象: 運用データを書き換える PR（設定の移行、基板 override の付け替え、校正 JSON の形式変更）
- 合格条件: 002 で、設定ページの値が更新前と一致し、1 回塗布できる

### A20 電源の入れ直し

- 操作: Pi の電源を入れ直す
- 合格条件: Moonraker の起動が遅れても、操作なしで使える状態へ戻る

### A21 所要時間の予測と実測

- 画面: `paste_solder`
- 前提: A8 と同じ基板と PCB。起動時の移行で `machine.toml` の `[tact]` から `travel_speed` / `travel_accel` / `z_speed` / `z_accel` が消え、`setup_sec` が `[solder.tact]` に残っている
- 操作: 実行前に予測所要時間を控え、A8 の塗布を通す。終わったらジョブコンソールの経過と見比べ、`solder.tact.setup_sec` を合わせる
- 合格条件: パッドを無効にすると予測が短くなる。予測と経過の差が、`solder.tact.setup_sec` を合わせた後に 1 割以内に収まる

### A22 機体の機能と [solder] への移行

- 前提: 移行前の `machine.toml`（`[paste_dispenser]` と `[tact]` の形）で、設定ページの全値を控えておく
- 操作: ブランチで backend を起動する
- 合格条件: `machine.toml` が `[solder]` の階層（`dispenser` / `nozzle` / `motion` / `defaults` / `auto` / `volume_correction` / `tact`）に移り、`config/history/machine-pre-N.toml` が残る。設定ページの全値が控えと一致する。機体一覧に「はんだ塗布」のバッジが出る。タブ（塗布 / 校正 / 基板 / 機体）、着地点（`paste_solder`）、塗布の画面が移行前と同じで、A8 と A18 が通る

## 未確認の項目（実機で確かめる）

既に main にある変更のうち、実機での確認が済んでいないもの。確かめたら、ここから消す。

- 静定待ちと検出サンプリング（`[settle]`、`[detection]`）
    - `settle.move_sec = 0.5` で、基準点移動とツールヘッドオフセットの静定が足りるか（以前は 1.0 s）
    - `detection.minimum_sample_count = 5` で、観測が失敗しないか（以前は 1）
    - `detection.sample_count = 10` で、位置合わせの再現性が落ちていないか（以前は 30）。INFO ログの有効検出数と標準偏差 X・Y で見る
- 微小線塗布（`micro_line`）
    - 始点の欠けと終点の過多が解消するか
    - `micro_line_z_start` / `micro_line_z_end`（既定は始点が自動、終点は始点に従う `start`）と `micro_line_settle_seconds`（既定 0.1 s）の値が合っているか
    - 最大速度で下降してもドームが崩れないか
- 終点高の既定を「始点に従う」に変えた: `micro_line_z_start` を数値にし `micro_line_z_end` を書いていない機体は、終点の Z が従来の auto 計算値から始点高の値に変わる。終点過多・始点欠けへの影響を見る
- 設定表の auto の高さの表示（選択欄の横の `(X)`。auto と、終点高の「始点に従う」）が、実際に塗るときのノズル高さと一致するか。G-code ジャーナルの Z と照らす
- 外周余白（`boundary_margin`）を負にしたとき、狙いどおりにはみ出して塗れるか。塗布量は pad 面積から決まるので、広げた経路では薄くなる。塗布量を増やすかは未決
- 大径側の外挿: 運転時の流量補正で、過吐出ぎみの `amount_ul` の点が採用されるか。`diameter_above_extrapolation_range` で外れた件数
- 塗布後の静定（`post_dispense_settle_seconds`、既定 0.1 s）で足りるか。流量キャリブレーションと塗布量校正を取り直す必要があるか
- セットアップの driver 選択: imx708 など ov9281 以外のカメラ、hifiberry 以外のスピーカーで、セットアップを通しで実行できるか。カメラ HAL の既定（解像度、モノクロ前提）が合うか
