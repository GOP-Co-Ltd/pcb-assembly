# MR6 中核 — 操作権リース (`src/web/api/control.py`)

MR4（frontend 分離）と並走するため、専用 worktree
`.claude/worktrees/agent-af6cb5e14409a78aa` で `control.py` とそのテストのみを
先行実装した。`Settings` / `app.py` / `dependencies.py` / router ゲート /
`get_identity` / WS gate / frontend は MR6 統合フェーズに残してある。

## 計画外の判断ログ

- **新規パッケージの `__init__.py` を 4 個追加した。** この worktree は MR3
  （`src/webui/` → `src/web/api/`）より前の main から分岐しているため
  `src/web/` が存在せず、`__init__.py` なしでは import できない。追加したのは
  `src/web/__init__.py` / `src/web/api/__init__.py` /
  `tests/web/__init__.py` / `tests/web/api/__init__.py`（後者 2 つは空。
  `tests/webui/` と同じ形）。いずれも新規ディレクトリなので MR3/MR4 の
  共有ファイルとは衝突しない（cherry-pick 時に MR3 側で既に作られていたら
  こちらを捨ててよい）。
- **失効の 2 条件の関係を「在線は切断猶予節だけを止める」と解釈した。** 計画書は
  「保持者の接続数 > 0 の間は無期限」と「切断猶予超過 OR（idle_timeout 超過
  かつ busy() が False）」を併記していて字面が衝突する。後者を literal に採り、
  在線中でも「無操作 600s かつ 非ジョブ中」なら失効する。理由: 逆の解釈
  （在線中は一切失効しない）だと、接続数 0 のときは 30s の切断猶予が先に効くので
  idle_timeout 節が到達不能な dead code になる。計画書のテスト項目
  「busy=True の間は idle_timeout 超過でも失効しない」も、在線中に idle 節が
  効くことを前提にしている。→ **orchestrator への確認事項**: 「操作者がタブを
  開いたまま 10 分席を外すと（ジョブ非実行なら）操作権が空く」という挙動で
  合っているか。意図と違えば idle 節を「接続数 0 のときだけ」に変える。
- **`claim` は保持者自身の呼び出しを許し、無操作タイマーを更新する。** 計画書の
  `ControlDep` は「認可チェックを Depends で行う」だけで専用メソッドを挙げて
  いないため、ゲート = `claim` で足りる形にした（空きなら取得、自分なら更新、
  他人なら `ControlDeniedError`）。同時に表示名も更新するので、後続の
  `POST /api/control/name` はこの `claim` に相乗りできる。
- **`LeaseInfo.connections` を追加した**（計画書が「必要なら足してよい」とした
  範囲）。保持者の WS 接続数。UI が「保持者はオフライン」を出せるほか、WS 在線の
  テストを内部属性ではなく公開 API 経由で書けるようにするため。
- **`on_change` は保持者が実際に変わったときだけ呼ぶ。** 保持者自身の再 `claim`
  （= 変更系リクエストごとに走る）で毎回ブロードキャストしないため。表示名の
  変更は「変わった」に含める。
- 失効判定は全公開メソッドの入口で行う（`connect` / `disconnect` を含む）。
  lazy 判定を 1 か所 `_expire_stale` に集約し、常に最新状態を見せる。

## 他 implementer への IF 変更通知

計画書のシグネチャからの逸脱はない。追加のみ:

- `LeaseInfo` に `connections: int` を追加（保持者の WS 接続数、未保持なら 0）
- `ControlDeniedError.holder` プロパティで保持者の表示名を取れる
  （`BusyError.owner` と同じ形）

## 既知の制約・残課題

- `busy()` はロック保持中に呼ばれる。注入するのは
  `lambda: state.busy_owner is not None`（`AppState._lock.locked()` を見るだけで
  ブロックしない）想定。ブロックしうる callable を渡してはいけない。
- 統合フェーズで必要な残作業: `Settings` への
  `control_disconnect_grace` / `control_idle_timeout`、`app.py` での
  `ControlLease(clock=time.monotonic, busy=..., on_change=...)` 構築、
  `dependencies.py` の `ControlDep`、`get_identity`、router ゲート、
  `routers/jobs.py:255` の `except` への `ControlDeniedError` 追加、frontend。
- この worktree は MR3 前の main から分岐しているので `src/webui/` が残っている。
  cherry-pick 先では `src/web/api/` 配下に素直に載る。

## 検証結果

- `uv run pre-commit run --files <対象 6 ファイル>`: pass
  （docformatter が日本語 description を再 wrap するため、説明は 1 行に収めた）
- `uv run pyright src/web/api/control.py tests/web/api/test_control.py`:
  0 errors
- `uv run pytest tests/web/api/test_control.py -m "not hardware"`: 33 passed
- `uv run pytest -m "not hardware and not e2e"`（全体）: 1707 passed
- 実機テストは未実行（`-m "not hardware"` を常に付与）

## mutation 確認

4 種の mutation を当てて、対応テストが落ちることを確認した（実行後に復元）。

| mutation | 落ちたテスト |
| --- | --- |
| `threading.Lock()` → `contextlib.nullcontext()` | `TestConcurrentClaim::test_only_one_of_eight_threads_takes_over_an_expired_lease`（8 スレッド全員が取得: `[8, 8, ...] != [1, 1, ...]`） |
| `_expire_stale` から `not self._busy()` を削除 | `TestIdleTimeout::test_busy_machine_keeps_the_lease_past_idle_timeout`, `test_lease_expires_once_the_job_finishes` |
| 切断猶予を接続数と無関係に `_last_active` から測る（在線判定を外す） | `TestWebsocketPresence` の 4 件 + `TestIdleTimeout` の 3 件 |
| `claim` の `_notify` をロック内に移動 | `TestOnChange::test_on_change_is_called_outside_the_lock` |

同時 claim の検出可能性は最初弱かった（空きリースへの 8 スレッド同時 claim は
critical section が短すぎて、ロックを外しても CPython が preempt せず素通りした）。
そこで「在線したまま無操作失効した保持者」を各ラウンドの初期状態にし、失効判定内で
呼ばれる `busy()` に GIL を明け渡す実装を注入するテストを足した。ロックがあれば
毎ラウンド勝者 1 本、無ければ 8 本になり、300 ラウンドでほぼ全ラウンドが検知される
（実測 300/300）。空きリースへの同時 claim テストも計画書の項目として残してある。
