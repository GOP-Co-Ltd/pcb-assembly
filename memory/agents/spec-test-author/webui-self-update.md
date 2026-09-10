# WebUI からのソフトウェア更新（webui-self-update）

計画書: `/home/gop/.claude/plans/claude-webui-api-ui-git-ssh-git-pull-web-robust-rocket.md`
ブランチ: `feature/2026-09-10/webui-self-update`

## 書いたテスト一覧

| ファイル | 件数 | 内容 |
| --- | --- | --- |
| `tests/test_web_service_script.py`（拡張） | +2（parametrize で 4 ケース） | `TestRenderUnit` に `TimeoutStopSec=15` / `StartLimitIntervalSec=0` |
| `tests/test_update_sudoers_script.py`（新規） | 13 | `render_sudoers` の argv 完全一致・`*` 禁止・install/remove・dispatch |
| `tests/web/selfupdate/conftest.py`（新規） | — | 実 git fixture（bare origin + clone + publisher）とスタブ実行ファイル生成 |
| `tests/web/selfupdate/test_repo.py`（新規） | 22 | `capture_state` / `fast_forward_blocker` / `fetch` / `merge_fast_forward` |
| `tests/web/selfupdate/test_steps.py`（新規） | 12 | `sync_command` / `smoke_command` / `restart_command` / `git_env` |
| `tests/web/selfupdate/test_runner.py`（新規） | 17 | 実行順序・失敗時の再起動抑止・楽観ロック・flock・タイムアウト・report 永続化 |

モックは 1 つも使っていない。git は実物（`tmp_path` 上の bare remote。ネットワークに出ない）、
`uv` / `sudo` / `systemctl` は `UpdateSettings` の絶対パス seam へ差し込んだ実スタブ実行ファイル。
`tests/helpers.py` への追加は無し（`before_deadline` / `wait_until` を利用しただけ）。

## 仕様根拠の対応表

| テスト | 計画書の根拠 |
| --- | --- |
| `TestRenderUnit::test_stop_timeout_is_bounded` / `test_restart_is_never_rate_limited` | 「4.」unit 定義に 2 行追加 |
| `TestRenderSudoers::test_allows_exactly_the_three_restart_invocations` | 「4.」sudoers の 3 変種と **順序も契約** |
| `TestRenderSudoers::test_contains_no_wildcard` | 「4.」ワイルドカードを 1 文字も使わない |
| `TestRenderSudoers::test_does_not_grant_a_shell` / `..._under_the_repository` | 却下案「`systemd-run`」「`web-service.sh` を許可」 |
| `TestInstallSudoers::test_nothing_is_installed_when_visudo_rejects_the_fragment` | 「4.」`visudo -cf` に必ず通してから設置 |
| `TestCaptureState` / `TestFastForwardBlocker` | 「1.」dirty 判定は `--untracked-files=no`、untracked は記録だけ／確定要件「ff 不可は何もせず中断」 |
| `TestFetch::test_unreachable_remote_returns_a_reason` | 「設計上の要 6.」fail-fast |
| `TestMergeFastForward::test_*_leaves_head_and_worktree_untouched` | 確定要件「何もせず中断」 |
| `TestSyncCommand::test_keeps_the_lockfile_immutable` / `..._extra_dependency_groups_installed` | 「設計上の要 4.」(a)(b) |
| `TestSmokeCommand` | 実行順序 5（import smoke） |
| `TestRestartCommand` | 「4.」固定 argv・`CANONICAL_ORDER` |
| `TestGitEnv` | 「設計上の要 6.」 |
| `TestSuccessfulRun` | 実行順序 0〜7 |
| `TestRestartIsWithheldOnFailure` | 確定要件「`uv sync` 失敗 → 再起動しない」＋ smoke 失敗も同様 |
| `TestStartIsRefused` | 安全弁 1（`expected_head`）/ 3（`enabled`）、実行順序 1（`restart_permitted`） |
| `TestSingleFlight` | 安全弁 2（flock。プロセスを跨ぐ） |
| `TestTimeout` | 「設計上の要 6.」timeout + `killpg` |
| `TestStatusAcrossProcesses` | 実行順序 6（report が唯一の永続状態） |

## 期待される失敗（すべて「実装がまだ無い」ことによるもの）

- `tests/web/selfupdate/` の 3 モジュールは `ModuleNotFoundError: No module named 'web.selfupdate'`
  で collect error（6 件 = モジュール 3 × doctest/通常の 2 経路）
- `tests/test_update_sudoers_script.py` の 13 件は `scripts/install-update-sudoers.sh` が
  無いため全滅（`FileNotFoundError` / bash の No such file）
- `tests/test_web_service_script.py` の追加 4 ケースは `render_unit` に 2 行が無いため失敗
- **既存の `tests/test_web_service_script.py` は 51 件すべて通ったまま**

## 実装側に求める仕様の確定（テスト側で解釈を決めた点。plan-implementer へ）

1. **`fetch()` / `merge_fast_forward()` の戻り値は `str | None`**（失敗理由 or None）。
   委譲プロンプトの型注記 `tuple[str | None]` は 1 要素タプルになっていて意味を成さないため、
   `fast_forward_blocker` と同じ「例外でなく `str | None`」規約に揃えた。
2. **`active_units()` と `restart_command(units)` の `units` はフル unit 名**
   （`pcbasm-api.service`）であってキー（`"api"`）ではない。sudoers の argv と直結するため。
   `UpdateReport.restart_units` も同じくフル unit 名。
3. **`capture_state` は `.git` でないディレクトリでも例外を投げず `(None, reason)`**
   （backend の `GET /api/update/status` が常に 200 を返せるように）。
4. **`UpdateRunner` が `state_dir` を作る**（テストは存在しないパスを渡す）。
5. **`status()` は実行前に `UpdateState.IDLE` の `UpdateReport` を返す**（report ファイル未作成でも例外にしない）。
6. **成功時の終状態は `RESTARTING`**（`schedule_restart` を予約した時点）。`SUCCEEDED` を
   いつ立てるかは計画書に無い。テストは `RESTARTING` だけを固定し、`SUCCEEDED` は縛っていない。
7. **`dirty` の中断理由に「未コミット」の語を含める**
   （`test_uncommitted_change_blocks` が substring で検証）。
8. **`install-update-sudoers.sh` の設置は `${SUDO} install -m 0440 "$tmp" "$path"`**。
   `-o root -g root` は付けない（`web-service.sh:182` の `install -m 0644` と同じ作法。
   `$SUDO` 経由で root として走るので所有者指定は冗長で、テストの no-op sudo seam では
   非 root の chown が失敗して観測できなくなる）。
9. **`install` は最後に `${SUDO} -n -l` で自己検査する**（テストの sudo スタブが `-n` を
   特別扱いして listing を返す前提）。`restart_permitted` も `sudo -n -l` の stdout に
   `<systemctl> restart --no-block <units>` の行があることで判定する想定。
   別の判定方法にするなら `tests/web/selfupdate/conftest.py::write_sudo_stub` を
   （テスト側の責任で）合わせる。
10. **スクリプトの関数名**は `render_sudoers` / `install_sudoers` / `remove_sudoers` / `main`、
    `source` されたら dispatch しない（`web-service.sh` と同じ）。

## 未検証（他担当・別レイヤ）

- `plan()` / `UpdatePlan` は公開 API だがフィールドが計画書で確定していないため未固定。
  router のテスト（`tests/web/api/routers/test_update.py`、別担当）で押さえる想定。
- 実 systemd の再起動、sudoers の実受理、実 `uv sync` はユーザーの実機確認事項（計画書に記載）。

## 検証結果

- `make format` 通過
- `uv run pytest tests/test_update_sudoers_script.py tests/test_web_service_script.py -m "not hardware" -q`
  → 21 failed（すべて未実装由来）, 51 passed
- `uv run pytest tests/web/selfupdate -m "not hardware"` → collect error 6 件（`web.selfupdate` 未実装）
- conftest の fixture / スタブ生成は単体スクリプトで実挙動を確認済み
  （git の ff・detached・unset-upstream、`uv` / `sudo -n -l` / `systemctl is-active` スタブ、
  `killpg` で `sleep` 子まで死ぬこと）
