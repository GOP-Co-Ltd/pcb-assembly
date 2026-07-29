# webui-audio-output — Phase A（pcbasm 通知音基盤）

計画書: `/home/gop/.claude/plans/claude-mr-141-main-webui-velvet-gem.md`（Phase A）
ブランチ: `feature/20260729/webui-audio-output`（worktree）

## 実装したもの

- `src/pcbasm/config.py` — `DEFAULT_AUDIO_DEVICE` / `DEFAULT_AUDIO_VOLUME`、`validate_audio_device` / `validate_audio_volume`、`Audio`、`Machine.audio`（常に `Audio` を返す）
- `src/pcbasm/hal/audio.py`（新規）— `Sound` / `AudioDevice` / `parse_aplay_devices` / `selectable_devices` / `AudioPlaybackError` / `AudioPlayer` / `AlsaAudioPlayer`
- `src/pcbasm/hal/sounds/{success,failure}.wav`（`src/webui/static/audio/` から `git mv`）
- `src/pcbasm/hal/__init__.py` — audio ブロックを re-export
- `tests/helpers.py` — `_skip_if_camera_unavailable` → `_skip_if_hardware_unavailable` 改名、`skip_if_no_alsa_audio`、`FAKE_AUDIO_DEVICES`、`FakeAudioPlayer`
- `tests/pcbasm/hal/test_audio.py`（新規）、`tests/pcbasm/test_config.py` の `TestAudio` / `TestMachineAudio`

## 計画外の判断

1. **`tests/webui/routers/test_pages.py::TestCompletionNotice::test_completion_sound_asset_is_served` を削除した**（計画では Phase B/C 扱い）。wav の `git mv` で `/static/audio/*.wav` が 404 になり、Phase A 単体で `make test-no-hardware` が赤くなるため。同クラスの `data-*-sound-url` アサーションは base.html 未変更で通るので手を付けていない（Phase B の担当範囲）。
2. **`src/webui/static/audio/` を `rmdir` した**。`git mv` で空になったディレクトリで、追跡対象ではないがディスクに残るため。
3. **`FAKE_AUDIO_DEVICES` を `tests/helpers.py` のモジュール定数として公開した**。`FakeAudioPlayer(devices=None)` の既定値であり、Phase B/C の WebUI テストがラベル・name を直接アサートするので定数参照できるほうがよい。
4. **`validate_audio_*` と `Audio` の配置は `class PadAlign` の直前**（`validate_paste_lift_height` が使用クラスの直前に置かれている既存の並びに合わせた）。定数は既存 `DEFAULT_*` 群の隣。
5. **`parse_aplay_devices` は「候補行の次のインデント行」をラベルにする**。記述行が 1 行も続かない候補は捨てる（実 `aplay -L` では必ず記述行が付く。起こり得ないケースの fallback は書かない方針）。

## IF 変更通知

なし（計画書のシグネチャどおり）。`Sound` は `pcbasm.hal.__all__` には入れていない（計画の re-export 一覧に無いため）。必要なら `from pcbasm.hal.audio import Sound`。

## 検証

`make format` / `make type` / `make test-no-hardware`（1644 passed, 89 deselected）すべてグリーン。
`-m hardware` の 2 件（`TestAlsaAudioPlayer::test_lists_default_and_plughw_devices` / `test_plays_success_and_failure_sounds`）は deselect を確認しただけで**実行していない**。

# webui-audio-output — Phase B / C（WebUI 配線・ジョブ完了音・開発ページ）

計画書: `/home/gop/.claude/plans/claude-mr-141-main-webui-velvet-gem.md`（Phase B / C + Phase D の e2e）
Phase D のドキュメント（README / install-softwares.sh / config テンプレート）は orchestrator 済みのため未着手。

## 実装したもの

Phase B:

- `src/webui/dependencies.py` — `get_audio_player` / `AudioPlayerDep`
- `src/webui/app.py` — `create_app(..., *, audio_player=None)`（None → `AlsaAudioPlayer()`）、`app.state.audio_player`、`JobManager(..., audio_player=...)`、`_lifespan` で `jobs.shutdown()` の後・`appstate.close()` の前に `close()`、`audio.router` を `settings_api` の直後に include
- `src/webui/jobs/manager.py` — `_play_completion_sound` / `_warn_completion_sound_failure`（`_park_machine` 直後・`publish_status()` の前）、`_logger`
- `src/webui/config_store.py` — `MACHINE_FIELDS` 末尾に `audio.device` / `audio.volume`、`_coerce` の `str` / `float` 分岐に `validate_audio_device` / `validate_audio_volume`
- `src/webui/routers/common.py` — `SECTION_LABELS["audio"] = "通知音"`
- `src/webui/templates/base.html` / `src/webui/static/js/job_console.js` — ブラウザ Web Audio を撤去（視覚通知は無変更）

Phase C:

- `src/webui/routers/audio.py`（新規）— `GET /api/audio/settings` / `POST /api/audio/test`
- `src/webui/routers/pages.py` — `TABS["dev"]` / `FEATURE_LABELS` / `FEATURE_TEMPLATES` に `audio`
- `src/webui/templates/dev/audio.html`（新規）、`src/webui/static/js/audio.js`（新規）

テスト: `tests/webui/conftest.py`（`audio_player` / `audio_client`）、`tests/webui/routers/test_audio.py`（新規）、`tests/webui/test_config_store.py::TestAudioFields`、`tests/webui/routers/test_settings_api.py`、`tests/webui/jobs/conftest.py`、`tests/webui/jobs/test_manager.py::TestAudioCompletionNotification`、`tests/webui/test_app.py`、`tests/webui/routers/test_pages.py`、`tests/e2e/conftest.py`、`tests/e2e/test_browser_ui.py::TestAudioPageOverBrowser`

## 計画外の判断

1. **`settings.js` は無変更**。`<select data-type="str">` は `scalarValue()` の `str` 分岐で、`type="range" data-type="float"` は `parseNumber()` 経路でそのまま扱えることを実装を読んで確認した（`input` イベントも既に張られている）。計画が言う「扱えないなら最小限の対応」は不要だった。JS から `select.value` / `range.value` を代入しても input/change は発火しないので、初期表示で誤保存も起きない。
2. **`dev/audio.html` のラベルは `<label for>` を control の兄弟に置いた**（`posctrl/copper_detection.html` の canny スライダーと同じ作法）。新しい CSS クラスは追加していない（既存 `status-card` のみで実際のブラウザ表示が崩れないことをスクリーンショットで確認済み）。
3. **`FakeAudioPlayer(playback_error=...)` に渡すのは `AudioPlaybackError`**。router の捕捉を `except (AudioPlaybackError, TimeoutError)` に narrow した計画に従うと、素の `RuntimeError` は 502 にならず 500 になるため（MR !141 は `except Exception` だった）。同じ理由で `test_manager.py` の再生失敗ケースも `AudioPlaybackError` を使う。
4. **`test_settings_api.py` の audio GET 期待値は `None`**（MR !141 は `"null"` / `1.0`）。fixture の `machine.toml` に `[audio]` を置かない方針なので欠落 → `None` が正しい。
5. **e2e の音量は `locator.fill("0.4")`**（Playwright は `input[type=range]` を「値を直接セットして input/change を発火」で扱う）。`step=0.05` に載る値なのでブラウザ側の丸めも起きない。
6. **`test_audio.py` に `TestDeviceListSource` を 1 クラス追加した**（計画のケース一覧には無い）。「router が絞り込まずプレイヤーの列挙結果をそのまま返す」＝薄ラッパー契約の回帰を止めるため。

## 既知の許容事項（計画書どおり）

- スライダー操作から debounce（350ms）以内にテスト再生を押すと、保存前の音量で鳴る
- ジョブ実行中は `PUT /api/settings/machine` が `machine_lock` で弾かれ保存失敗トーストが出る（他の設定と同一挙動）

## IF 変更通知

なし。`create_app` / `JobManager` の追加引数はいずれも keyword-only + 既定 `None` で、既存呼び出しは無変更で動く。

## 検証

`make format` / `make type` / `make test-no-hardware`（1680 passed, 91 deselected）/ `make test-e2e`（53 passed）すべてグリーン。`grep -rn '</content>' src/ tests/` は空。
`make test` / `-m hardware` は実行していない。

# webui-audio-output — レビュー指摘の修正（code-reviewer 差し戻し）

レビュー記録: `memory/agents/code-reviewer/webui-audio-output.md`。orchestrator が採用した 7 件のみ対応した。

## must-fix

1. **既定 `app` fixture に `FakeAudioPlayer` を注入した**（`tests/webui/conftest.py`）。従来は `create_app(webui_settings)` で実 `AlsaAudioPlayer` が入り、`notify_on_completion=True` の合成ジョブが完了時に実 `aplay` を起動していた。計画書の「既定 `app` fixture は無変更」から意図的に逸脱。
    - `audio_player` fixture（function scope）を `app` の依存にしたので、`played` を検証するテストは毎回新しい fake を得る（他テストの再生要求で汚染されない）
    - 重複していた `audio_client` fixture は削除し、`tests/webui/routers/test_audio.py` は既定の `client` を使う形に変更
    - 別アプリを作る `fake_camera_app` / `checkerboard_camera_app` にも同じ fake を注入した（実 `AlsaAudioPlayer` を非 hardware 経路で作らないため）。`real_client`（`@mark_hardware` 専用）は実プレイヤーのまま
    - `src/` は無変更

## should-fix

2. **e2e の wav アサーションを `".wav" not in url` にした**（`tests/e2e/test_browser_ui.py`）。`_static_asset_url` が `?v=<mtime>` を付けるため `endswith(".wav")` は常に False で、ブラウザ Web Audio の復活を検出できなかった。
3. **`AlsaAudioPlayer.play` が `submit` の `RuntimeError` を捕捉するようにした**（`src/pcbasm/hal/audio.py`）。`close()` と競合しても「常に `Future` を返す」契約を守る。失敗済み Future の生成は `_closed_player_future()` に切り出して `_closed` 分岐と共用。テストは `TestAlsaAudioPlayer::test_play_racing_with_close_still_returns_failed_future` を追加。
4. **`_warn_completion_sound_failure` の `CancelledError` 分岐を削除した**（`src/webui/jobs/manager.py`）。cancel 経路が無く到達不能。`CancelledError` の import も落とした。

## nit

5. **`Audio.__attrs_post_init__` の `isinstance(self.device, str)` を削除した**（`src/pcbasm/config.py`）。`volume` 側の `isinstance`（bool / 非数値を弾く）は残置。
6. **`audio.js` の早期 return に `volumeOutput === null` を追加した**。
7. **不正フォーマット WAV のテストを追加した**（`TestAlsaAudioPlayer::test_rejects_sound_file_that_is_not_16bit_pcm`）。tmp_path に 8-bit PCM WAV を `wave` で生成し、`monkeypatch.setitem` で `_SOUND_PATHS["success"]` を差し替えて `play()` が `AudioPlaybackError` になることを確認する（`aplay` には到達しない）。

## 計画外の判断

1. **テストから private を触る 2 箇所に `# pyright: ignore[reportPrivateUsage]` を付けた**（`player._closed` / `audio_module._SOUND_PATHS`）。ホワイトボックスの再現に必要で、リポジトリ全体が pyright warning 0 件のため無視コメントで抑えた（`make type` は 0 errors / 0 warnings）。

## 検証

- `make format`（`pre-commit run -a`）と、未追跡ファイルへの `pre-commit run --files ...` の両方がグリーン
- `make type` — 0 errors, 0 warnings
- `make test-no-hardware` — 1682 passed, 91 deselected
- `make test-e2e` — 53 passed
- **実 `aplay` 起動 0 件**を実測。PATH 先頭に「引数を記録して stdin を捨てるだけの `aplay` shim」を置いて `make test-no-hardware` / `make test-e2e` を全量実行し、記録が 0 行であることを確認した。逆に `app` fixture の注入を一時的に戻すと同 shim が 2 件（`test_jobs.py` の notify 系 2 テスト）を記録したので、検出力があることも確認済み。shim は scratchpad に作り、実行後に削除した
- `grep -rn '</content>' src/ tests/` は空
- `make test` / `-m hardware` は実行していない
