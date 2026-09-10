# WebUI からのソフトウェア更新（webui-self-update）— 実装の判断ログ

計画書: `/home/gop/.claude/plans/claude-webui-api-ui-git-ssh-git-pull-web-robust-rocket.md`
spec-test-author ノート: `memory/agents/spec-test-author/webui-self-update.md`
ブランチ: `feature/2026-09-10/webui-self-update`

## 計画から逸脱した点（すべて意図的）

### 1. `run_command` / `CommandResult` / `tail` を `steps.py` に置いた

計画書の `steps.py` は「argv を組み立てるだけの純関数」だが、外部プロセスの実行
（`stdin=DEVNULL` / timeout / `start_new_session` + `killpg`）を `repo` / `service` /
`runner` の 3 つが共有する。7 つ目のモジュールを増やすより「外部コマンドを組み立てる
場所と走らせる場所を 1 つにする」方が読みやすいと判断した。依存の向きは
`settings → steps → repo/service → report → runner` の一方向のまま。

### 2. `UpdatePlan` と API 表現（pydantic）への変換を `report.py` に置いた

backend の `routers/update.py` と frontend の `update_api.py` が**同じ 1 つの変換**を
使う必要がある（表示文字列をサーバ側で組む規約）。`runner.py` に置くと router 側で
2 箇所に分かれ、独立モジュールにすると計画書に無い 7 つ目が増える。`report.py` の
docstring に理由を明記した。`web.selfupdate` → `web.api.models` の依存が増えるが、
`models.py` は pydantic + 標準ライブラリのみなので import 契約は保たれる
（`tests/test_package.py::TestSelfUpdateImportLight` が機械検証）。

### 3. `schedule_restart` はスレッドを起こさず、呼び出し元スレッドで待ってから投げる

計画書は「daemon thread から」だが、`start()` は既に別の daemon thread（更新スレッド）
で走っており、そこで `restart_delay` 秒待てば「202 をフラッシュして 1 回ポーリング
させる」目的は同じく達成できる。さらに契約テスト
`TestSuccessfulRun::test_external_commands_are_invoked_in_order` は `runner.wait()` の
直後にログを読むので、別スレッドに逃がすと restart 行がまだ無く落ちる。スレッドを
1 本減らして docstring に「リクエストスレッドから呼ばない」と明記した。

### 4. `UpdateRunner.__init__` は `state_dir` を作らない（実行時に作る）

spec-test-author ノート 4 は「`UpdateRunner` が `state_dir` を作る」だが、`__init__`
で作ると **アプリを生成しただけでリポジトリの `data/selfupdate/` が生える**
（更新を一度も使わない frontend のテスト全部が実リポジトリを汚した）。`start()` の
先頭で `mkdir(parents=True, exist_ok=True)` に移した。契約テストは
「存在しないパスを渡して start / status できる」ことだけを見ているので全件緑のまま。

### 5. `UpdateState.SUCCEEDED` を追加した

spec-test-author ノート 6 は「成功時の終状態は `RESTARTING`」で `SUCCEEDED` を縛って
いない。**再起動対象の unit が 1 つも active でないホスト**（開発機・手元起動）では
再起動を投げないので `RESTARTING` のままだと嘘になる。`units` が空のときだけ
`SUCCEEDED` にする。unit がある通常の機体では従来どおり `RESTARTING`。

### 6. `active_units()` をプロセス生存中キャッシュする

`GET status` は 1 秒間隔でポーリングされる。毎回 `systemctl is-active` を 2 回起こす
のは Pi では無駄。unit 構成が変わるのは install / 更新のときで、そのときはこの
プロセス自身が再起動している。

### 7. `app.js` に `frontendApi(method, url, body)` を足した

`tests/web/ui/test_layout.py::TestMachinePrefixFunnel::test_fetch_is_confined_to_the_funnel`
が「`fetch(` を書いてよいのは `app.js` だけ」を機械検証している。frontend 自身の
`/api/self-update/**` は POST も要るので、既存の `frontendJson`（GET 専用）を
`frontendApi` の薄いラッパに変え、`update.js` は funnel 経由で叩く。

### 8. `StartLimitIntervalSec` は `[Unit]` セクションに置いた

systemd v229 以降 `StartLimitIntervalSec` / `StartLimitBurst` は `[Unit]` が正。
`[Service]` に書いても後方互換で読まれるが警告が出る。契約テスト
（`render_unit` の出力に文字列が含まれること）はどちらでも通る。

### 9. `summary` フィールドをレスポンスに追加した

「N 件の更新があります」「最新です」の組み立てを JS でやると
`webui-thin-wrapper`（表示文字列はサーバ側）に反するため、`UpdateStatusResponse.summary`
としてサーバが返す。`restart_notice` と同じ扱い。

### 10. 更新パネルを `partials/update_panel.html` に切り出した

計画書はテンプレート 2 枚（`dev/update.html` と `update.html`）。両方が同じ DOM と
同じ `update.js` を使うので、パネル本体は partial 1 枚にして include する。
`data-requires-control` は include 側の `update_requires_control` で出し分ける
（frontend 自身のページに付けると、操作権の無い層で control.js が永久に `inert` を
付けてボタンが死ぬ）。

## spec-test-author のテストで誤りと判断した箇所

**なし。** 56 件すべて実装側を合わせて緑にした。1 件だけ解釈が要ったのは
`TestSuccessfulRun::test_external_commands_are_invoked_in_order`（`runner.wait()` の
直後に restart 行がログに載っていることを要求する）で、これは上記 3 の設計判断で
満たした。テストは変更していない。

## 触った既存テスト（保護対象外・理由あり）

- `tests/web/ui/test_control_ui.py`: `_GATED_ELEMENTS` に
    `"partials/update_panel.html": {".update-actions"}` を追加。この辞書は
    「ゲート対象の在庫表」で、新しいゲート要素を足したら更新するのが本来の運用
    （更新しないと `test_no_other_template_gates_anything` が漏れとして検出する）
- `tests/web/ui/conftest.py`: `ui_settings` に `update_state_dir=tmp_path/"selfupdate"`。
    frontend app を作るテストがリポジトリの `data/` に書かないようにする衛生措置

## 検証できていない範囲（ユーザーの実機確認事項）

計画書「検証」節のとおり:

1. 実 systemd の再起動（`systemctl restart --no-block` が本当に完走するか）
2. sudoers が実際に受理されること（`./scripts/install-update-sudoers.sh install` →
   `sudo -n -l`）
3. ネットワーク越しの `uv sync`（GitLab からの実 fetch を含む）
4. SIGTERM 中に 202 を返しきること
5. frontend が本当に落ちて戻る間のブラウザ挙動（`/update` を最後に試す）
6. **既存機体は `./scripts/web-service.sh install <target>` の再実行が必要**
   （unit 定義に `TimeoutStopSec=15` / `StartLimitIntervalSec=0` を足したため）

## 環境メモ

この Pi には torch / optuna が無いので `make test-no-hardware` は `tests/ml` の
collect で 73 error になる（**この変更の前から同じ**。`git stash` して確認済み）。
実質の検証は `uv run pytest --ignore=tests/ml -m "not hardware and not e2e"`。
`make type` も同じ理由で ML 由来の 133 error が出るが、`src/web` / `tests/web` /
`tests/e2e` に限れば 0 error。


---

# code-reviewer 差し戻しへの対応（2 巡目）

指摘: `memory/agents/code-reviewer/webui-self-update.md`（verdict: request-changes）

## must-fix

- **M1 修正。** `SMOKE_MODULES`（unit → モジュール）と `smoke_modules(units)` を追加し、
    smoke の対象を active unit から導出する。`import web.api.app` が
    cv2 / pcbnew / picamera2 を引くことは実測で確認した（`web.ui.app` は 0 件）。
    active unit が 0 のホストでは smoke を skip し、手順は「省略しました」として記録する
    （画面の 5 段構成を崩さない）。
    **契約テストとの衝突は回避できた**: `smoke_command(settings)` の引数は
    `modules` を既定付きで足したので、`tests/web/selfupdate/test_steps.py` が固定する
    「既定は `import web.api.app, web.ui.app`」はそのまま成立する。テストは未変更。
- **M2 部分的に別解。** `sudo -n -l` を `COLUMNS=1000` 付きで起動し、さらに照合前に
    折り返しを畳んで空白を正規化する（`_unwrapped`）。**指示された
    `sudo -n -l <command...>` の終了コード判定は採れなかった**（下記「契約との衝突」）。
    `scripts/install-update-sudoers.sh` の `verify_sudoers` は衝突が無いので
    **指示どおり終了コード判定**に変えた。
- **M3 修正。** `merge_timeout: float = 600.0` を追加して `merge_fast_forward` に適用。
    `git_timeout=30.0` は rev-parse / status などローカル完結の読み取り専用に残した。
    docstring は「git が自分で断った場合は 1 バイトも動かない。checkout 途中で
    打ち切られた場合はこの限りではない」と正確に書き直し、タイムアウト時のメッセージに
    `git status` / `git lfs pull` の確認を入れた。

## should-fix（全件対応）

- **S1** `_run` に `except Exception` を足し、`report.fail()` で残して保存する。
    併せて `restart_units()` の `active_units` を `except OSError` で `()` に倒し、
    `GET /api/update/status` の「常に 200」を守る。
- **S2** `restart_permitted` は `units` が空なら即 `None`（再起動しないので許可は不要）。
    これで `SUCCEEDED` が到達可能になり、テストも足した。
- **S3** `current_suffix="dev/update"` に修正。
- **S4** backend の `update_dir` を `data_dir` 由来から **リポジトリ直下固定**へ変更
    （ロックが守るのは worktree）。frontend 側と同じ既定になる。テスト fixture
    （`tests/web/api/conftest.py` / `tests/web/ui/conftest.py` / `tests/e2e/conftest.py`）は
    tmp を明示して、リポジトリの `data/` を汚さないようにした。
- **S5** `schedule_restart` が `str | None` を返し、失敗を report に書き戻す。
- **S6** `stale_units()` / `stale_unit_warning()` を実装。`web-service.sh` を source して
    `render_unit` の出力と設置済み unit を比べるだけ（**非特権の読み取りのみ、自動
    install はしない**）。`UpdateReport.warnings` → `UpdateRunInfo.warnings` →
    `#update-warnings` で画面に出す。この機体で実行して
    `('pcbasm-api.service',)` が返ることを確認済み（まさに本 MR が unit を変えたため）。
- **S7** `git status` / `git ls-files` の失敗を検査し、`(None, 理由)` を返す。
- **S8** `check_fetch_timeout: float = 20.0` を追加し、`check()` の fetch に適用。
- **S9** `tests/web/test_update_host_shapes.py`（新規 15 件）で
    frontend 専用機 / 機体単体 / unit 0 台 / 折り返し listing / 再起動拒否 /
    実行ファイル欠如を固定。`next(_client(...))` は `@contextmanager` の
    `closing_client` に置き換えて lifespan を閉じる。
    **`test_runner.py:341-344` の条件付き assert は spec-test-author の契約なので触っていない**
    （実装は「units が空でなければ preflight で中断」に確定した）。
- **S10** `head_label` と `failed_detail` をサーバ側で組んで返し、JS の連結・再導出を消した。

## nit

- `tail(text, 0)` が全文を返す → `lines <= 0` は空文字に修正
- `uv sync` / smoke に `git_env()` を適用
- README の `*_UV_SYNC_ARGS` を「丸ごと置き換える」と明記（`--locked` を落とす事故の注意付き）
- `/update` への UI 導線: `dev/update.html` から `/update` へ、`update.html` から
    各機体の `/m/<id>/dev/update` へ相互リンクを追加
- `_dirty_path` の `core.quotepath` は復号しない旨を docstring に明記（表示専用）
- docformatter が割った docstring は format 実行で追随
- 未対応: `wait()` が public（テスト同期専用）、`update_support.py` と
    `conftest.py` の origin/clone 生成の重複。どちらも保護対象ファイルに手を入れずに
    直せないか、直すと契約テスト側の import が壊れるため見送った

## 契約との衝突（M2。orchestrator の裁定を仰ぎたい）

指示は「`sudo -n -l <command...>` の**終了コードだけ**で判定」だった。これを
`restart_permitted` に入れると、スタブが記録する呼び出しログに
`sudo -n -l /path/systemctl restart --no-block ...` という行が残る。
`tests/web/selfupdate/conftest.py:267` の `restart_calls()` は
**「restart を含む行」**を再起動とみなすため、preflight を通っただけで
「再起動した」と判定され、`restart_calls(...) == []` を要求する契約テスト
**6 箇所**（`test_runner.py:253, 269, 291, 306, 348, 420`）が落ちる。
代表例は `TestRestartIsWithheldOnFailure::test_failing_uv_sync_skips_both_the_smoke_and_the_restart`
（uv sync 失敗時に再起動しないことの直接検証）。

そこで、**code-reviewer 自身が代替として挙げていた**
「`COLUMNS` を広く与える／照合前に正規化する」（レビュー本文 M2 の「修正方向」）を採り、
両方を実装した。回帰は自前のテストで固定してある:
`tests/web/update_support.py::write_sudo_stub(wrap=True)` が実 sudo と同じ 80 桁
折り返しを再現し、`TestWrappedSudoListing` が preflight を通ることと
「許可が無い場合は依然として落ちる」ことの両方を見る。
修正前のコードではこのスタブに対して substring 照合が `False` になることを確認済み。

`restart_calls()` の定義を変えてよい（= spec-test-author に差し戻す）なら、
指示どおりの終了コード判定へ寄せられる。


---

# approve 後の残件対応（3 巡目）

## R1〜R5

- **R1 修正。** `verify_sudoers` のループを 3 変種
    （api / ui / `api ui`）に拡張。同居機が実際に使う 3 つ目を検査していなかったので、
    「install は成功、実行時の preflight で落ちる」が残っていた。
- **R2 修正。** `stale_units()` を `try/except OSError` で包み、失敗は警告の欠落に留める。
    警告のための読み取りが更新本体（と再起動）を殺さない。
- **R3 修正。** README の `PCBASM_API_UPDATE_STATE_DIR` の既定を
    「リポジトリ直下の `data/selfupdate`。`PCBASM_API_DATA_DIR` では動かない」に訂正。
- **R4 修正。** `run.warnings ?? []`。ついでに `run.steps ?? []` と
    `run.failed_detail ?? ""` も同じ理由で守った（`pollOnce` の try の外で投げると
    ポーリングが無言で止まる）。
- **R5 修正。** `bash -c` の f-string を廃止。`scripts/web-service.sh` に
    **`render <target>`** サブコマンド（stdout に unit テキストを書くだけ。
    `require_privileged_tools` を通らない読み取り専用）を足し、
    `run_command((script, "render", target))` と argv だけで呼ぶ。
    `bash` への依存も消えた。

## nit

- **`is-active` の判定を見直した。** `RESTARTABLE_STATES =
    {active, activating, reloading, failed}`。**`failed` を含めるのが要点**で、
    起動に失敗したリビジョンを直して更新し直すとき、落ちている unit を対象から外すと
    修正が永久に適用されない（計画書「既知のリスク 1」からの復帰経路）。
    `inactive` / `deactivating` は意図的に止めているので起こさない。
- **`git` が無いホストで 500 になる件を修正。** `run_command` の `Popen` を
    `except OSError` で包み、失敗した `CommandResult` として返す。docstring の
    「例外を投げない」が実際に真になった。`systemctl` だけの場当たり防御も残してある。
- `_wrapped()` スタブに実 sudo の継続文字 `\` を追加（`tests/web/update_support.py` は
    自分の担当ファイル）。
- `tests/web/test_update_host_shapes.py` の冒頭にミラーレイアウト外の理由を明記し、
    3 連続空行を解消。
- `UpdateSettings.unit_dir` を seam として追加（既定 `/etc/systemd/system`）。
    これで unit 差分チェック（S6）が実機に触らず本当にテストできるようになり、
    `TestUnitDriftWarning` が「古い unit → 警告 1 件・更新は成功」「最新 → 警告なし」
    「読めない → 警告なしで更新継続」を固定する。

## 直さず報告（契約・保護対象との衝突）

1. **`smoke_command` の既定引数は外せない。** `tests/web/selfupdate/test_steps.py`
   （保護対象）の `TestSmokeCommand` が `smoke_command(settings)` を引数なしで呼び、
   既定が `import web.api.app, web.ui.app` であることを固定している。必須引数にすると
   TypeError で 2 件落ちる。代わりに docstring へ
   「**本番は必ず `smoke_modules(active_units)` の結果を渡す**」と明記し、
   `tests/web/test_update_host_shapes.py::TestSmokeTargetsFollowTheHost` が
   専用機・機体単体の両方で実際の argv を固定して M1 の再発を検出する。
   契約側を変えてよいなら必須引数化が正しい。
2. **`tests/web/selfupdate/conftest.py` への「忠実版は update_support.py」注記**は
   保護対象ファイルなので入れていない。代わりに `tests/web/update_support.py` の
   `write_sudo_stub` / `write_systemctl_stub` の docstring に
   「conftest のものは折り返さない／常に active を返す」と対比を書いた。
