# webui-audio-output レビュー

計画書: `/home/gop/.claude/plans/claude-mr-141-main-webui-velvet-gem.md`
対象: worktree `feature+20260729+webui-audio-output` の作業ツリー（**commit されていない**ため
`git diff main...HEAD` は空。`git diff main -M` + 未追跡 6 ファイルをレビューした）

## verdict: request-changes

must-fix 1 件（`make test-no-hardware` が実 `aplay` を起動し実スピーカーを鳴らす）。
それ以外は計画準拠で、仕様・薄ラッパー契約・カプセル化に問題なし。

## must-fix

### 1. `make test-no-hardware` が実 `aplay` を 2 回起動し、実スピーカーから成功音が鳴る

- 対象: `tests/webui/conftest.py:133`（`app` fixture）/ 影響テスト
  `tests/webui/routers/test_jobs.py:206-214`、`tests/webui/routers/test_jobs.py:673-690`
- 問題: `app` fixture が `create_app(webui_settings)` を audio_player 無しで呼ぶため実
  `AlsaAudioPlayer` が入る。上記 2 テストは `notify_on_completion=True` の合成ジョブを
  SUCCEEDED まで走らせるので `JobManager._play_completion_sound` →
  `subprocess.run(["aplay","-q","-D","default"], input=success.wav)` が実行される。
- 根拠（実測）: `aplay` を「呼び出しを記録して stdin を捨てる」shim に差し替えて
  `pytest -m "not hardware and not e2e"` を全量実行 → **`aplay called: -q -D default` が 2 件**。
  `tests/webui` 単体でも 2 件。この環境には `/usr/bin/aplay` が存在するため、shim 無しでは
  実プロセスが起動し、スピーカー接続時は 4.8 秒の成功音が 2 回鳴る。
  併せて lifespan teardown の `AlsaAudioPlayer.close()` が `shutdown(wait=True)` で
  再生完了まで待つので、実測ではないが 1 件あたり数秒（デバイス busy 時は最大 30 秒）
  `make test-no-hardware` が延びる。
- 規約違反: `memory/feedback-no-hardware-test-execution.md`（実機を動かすものは
  `@mark_hardware` に隔離し `make test-no-hardware` から外す）/ skill `testing-strategy`。
  計画書 174 行の「既定 `app` fixture は無変更」は計画側の欠陥で、ここは逸脱が必要。
- 修正方向: `app` fixture に `FakeAudioPlayer` を注入する（`audio_client` /
  `audio_player` fixture との重複整理も併せて）。`src/` は変更不要。
- 確信度: 高（実測で再現）

## should-fix

### 2. e2e の「wav を要求しない」アサーションが原理的に失敗しない

- 対象: `tests/e2e/test_browser_ui.py:76` `assert not any(url.endswith(".wav") for url in requested_urls)`
- 問題: 静的 URL は `_static_asset_url`（`src/webui/app.py:56-60`）が `?v=<mtime_ns>` を
  付けるため、wav を取得しても URL は `.wav?v=123...` で終わり `endswith(".wav")` は常に False。
  Web Audio 復活の回帰を検出できない。`".wav" not in url` にすべき
  （`tests/webui/routers/test_pages.py:243` 側は `".wav" not in text` で正しい）。
- 確信度: 高

### 3. `AlsaAudioPlayer.play()` が `close()` と競合すると Future を返さず素の `RuntimeError` を投げる

- 対象: `src/pcbasm/hal/audio.py:125-131`（`play`）/ `134-138`（`close`）
- 問題: `self._closed` の判定と `self._executor.submit()` の間に別スレッドの `close()` が
  入ると `submit` が `RuntimeError("cannot schedule new futures after shutdown")` を
  **同期的に** raise する。`AudioPlaybackError` ではないので
  `src/webui/routers/audio.py:73` の `except (AudioPlaybackError, TimeoutError)` を抜け 500 になる
  （`JobManager` 側は `except Exception` なので影響なし）。「常に Future を返す」ABC 契約も崩れる。
- 窓は「lifespan shutdown 中に `/api/audio/test` が処理中」だけで実運用ではほぼ起きない。
  `_closed` チェックと `submit` を lock で囲む、または `submit` の `RuntimeError` を
  `AudioPlaybackError` に包む。
- 確信度: 中（コード上は確実、発生確率が低い）

### 4. `_warn_completion_sound_failure` の `CancelledError` 分岐が到達不能

- 対象: `src/webui/jobs/manager.py:722-728`（+ `manager.py:15` の `CancelledError` import）
- 問題: この Future を cancel する経路が無い（`close()` は `shutdown(wait=True)` で
  `cancel_futures` を使わない。`FakeAudioPlayer` も完了済み Future を返す）。
  CLAUDE.md 開発原則 2「起こり得ないシナリオに対するエラーハンドリングは書かない」に反し、
  テストも無い。`future.exception()` を直接読むだけで足りる。
- 確信度: 中（cancel 経路が無いことは grep で確認）

## nit

5. `src/pcbasm/config.py:71` `if not isinstance(self.device, str)` は到達不能
   （型注釈経路では通らず、cattrs は `str` フィールドを `str(value)` で強制変換する）。
   メッセージも `validate_audio_device` と重複。
6. `src/pcbasm/hal/audio.py:119,121` で `parse_aplay_devices("")` を 2 回書いている。
   フォールバックを 1 箇所にまとめられる。
7. `src/webui/routers/audio.py:44` `AudioTestResponse.played` は常に `True`（失敗は 502）で
   情報量がない。計画どおりなので任意。
8. 通知音の再生失敗が `webui.jobs.manager` ロガーにしか出ず、ジョブコンソール
   （`runtime.log`）には出ない（`src/webui/jobs/manager.py:707-720`）。操作者はスピーカー故障に
   気付けず `/dev/audio` のテスト再生に頼ることになる。計画書 102 行どおりなので任意。
9. `src/webui/static/js/audio.js:10,15` — `volumeOutput` だけ null ガードが無い
   （`deviceSelect` / `volumeRange` はガード済み）。
10. `machine.toml` を手編集して `volume = 0.77` のような step 外の値にすると
    `<input type="range" step="0.05">` が表示値を丸め、サーバ値と表示が食い違う（保存はされない）。
11. `data/config-templates/kurousagi.paste/machine.toml:93` のコメント
    「ここへ書き戻される」は誤読を招く（書き戻し先は `config/machine.toml`。テンプレートではない）。
12. import 元が不統一: `tests/helpers.py` / `tests/pcbasm/hal/test_audio.py` は
    `pcbasm.hal.audio` から、`tests/webui/routers/test_audio.py` は `pcbasm.hal` から取っている。
13. `_wav_with_gain` のフォーマット検証（`sampwidth != 2` / `comptype != "NONE"`）は
    音声パスがモジュール定数のため単体で踏めず未検証。差し替えミス時の
    `AudioPlaybackError` は実機のテスト再生でしか確認できない（注入口を作るのは過剰なので現状で妥当）。

## 確認して問題がなかった主な点

- 計画の公開 IF（`Audio` / `Machine.audio` が `None` を返さない / `parse_aplay_devices` /
  `selectable_devices` / `AudioPlayer` ABC / `/api/audio` prefix / 例外 narrow / 400 撤去）は一致
- 除外ルール（`hw:` / `dmix:` / `sysdefault:` / `hdmi:` / `default:CARD=` / `DEV=1`）と
  `default` 先頭合成、未検出補完は仕様どおり
- `_play_completion_sound` の挿入位置（`_park_machine` 直後・`publish_status()` の前）、
  ABORTED / `notify_on_completion=False` で鳴らさない、再生失敗が status を汚さない
- `config_store._coerce` の validator 適用（`float` 分岐に `audio.volume`、`str` 分岐に
  `audio.device` + strip）。nan / 範囲外 / 空白のみが 400 になる
- `selectable_devices` の結果と `audio.js` の `<option>` 生成（`select.value` が必ず候補に存在）
- 薄ラッパー契約: `audio.js` は DOM / fetch / `%` 整形のみ、`routers/audio.py` は算出なし、
  保存は既存 `settings.js`（`<select data-type="str">` / `range data-type="float"` の経路確認済み）
- ブラウザ Web Audio 撤去の完全性: `static/audio/` 消滅、`audioContext` 等の残骸なし、
  視覚通知（`showCompletionNotice` / タイトル / dismiss）は無変更
- `@mark_hardware` の 2 件は deselect（91 deselected = main +2）
- `src/pcbasm/hal/sounds/` は git-lfs 追跡（`.gitattributes` の `*.wav`）、`_SOUNDS_DIR` は cwd 非依存
- ドキュメント（README / config-templates / install-softwares.sh）は実装と一致
- `grep -rn '</content>' src/ tests/` は空

## 検証結果

- make format: pass（ただし `pre-commit run -a` は **未追跡ファイルを対象外にする**ため、
  新規 6 ファイルは `pre-commit run --files ...` で個別に実行して pass を確認した）
- make type: pass（pyright 0 errors）
- make test-no-hardware: pass（1680 passed, 91 deselected）。
  **実行時は `aplay` shim を PATH に差し込み、実スピーカーを鳴らさずに実施した**（must-fix 1 参照）
- make test-e2e: 未実行（orchestrator 報告の 53 passed を採用）
- `make test` / `-m hardware`: 未実行（規約どおり）
