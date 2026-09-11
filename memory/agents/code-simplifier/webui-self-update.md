# WebUI からのソフトウェア更新（webui-self-update）— 簡素化とドキュメント同期

計画書: `/home/gop/.claude/plans/claude-webui-api-ui-git-ssh-git-pull-web-robust-rocket.md`
前段: `memory/agents/code-reviewer/webui-self-update.md`（2 巡目 approve）/
`memory/agents/plan-implementer/webui-self-update.md`（3 巡分の判断ログ）

## 簡素化した内部実装

### 1. `runner._execute` の「記録 → 保存 → 失敗なら return」の 3 重複を潰した

`src/web/selfupdate/runner.py`

```python
report.record(STEP, reason)
self._save(report)
if report.state is UpdateState.FAILED:
    return
```

が PREFLIGHT / FETCH / MERGE の 3 箇所に逐語で並んでいた（12 行）。
`_step(report, step, reason, detail="") -> bool` を切って `if not self._step(...): return`
の 1 行にした。既にあった `_step_command`（外部コマンド版）は
「`CommandResult` → 失敗理由の文字列」の変換だけを担当し、記録と保存は `_step` に委譲する。

- 「保存を挟むのは、この直後にプロセスが死んでも『どこで止まったか』が残るため」という
    非自明な理由が 1 箇所（`_step` の docstring）に集約された。以前は 3 箇所の暗黙知
- SMOKE を skip するホスト（unit 0 台）の `report.record` も `_step` に寄せて、
    5 手順すべてが同じ 1 本の経路を通るようにした
- **`report.record` / `UpdateReport` の公開 IF は不変**（`_step` は `record` を呼ぶだけ）

### 2. `start()` が git を 2 回起こしていたのをやめた

`_refuse(expected_head)` が `capture_state` を呼んだ直後に `_new_report()` がもう一度
`capture_state` を呼んでいた（1 回あたり git 5〜7 プロセス）。`_refuse` が
`tuple[RepoState | None, str | None]` を返すようにし、`_new_report(state)` がその値を使う。

- git の起動が 1 回分減る（Pi では体感できる差）
- 副次的に **判定した HEAD と report の `from_head` が必ず一致する**。以前は 2 回の
    観測の間に tree が動くと食い違いえた（楽観ロックの `expected_head` を検証した HEAD と、
    復旧手順で使う「更新前 commit」がずれる）
- どちらも private メソッドなので公開 IF は不変

### 3. `repo.py` の upstream 問い合わせの重複を関数化

`_git(settings, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")` を
`_optional` で包む 3 行が `capture_state` と `merge_fast_forward` に逐語で 2 回あった。
`_upstream(settings) -> str | None` に抽出。「upstream の引き方」が 1 箇所になり、
`merge_fast_forward` が「observe → merge」の 2 行に縮んだ。

なお「repo.py の失敗検査が重複していないか」は確認した結果 **重複ではない**。
`capture_state` の `status` / `ls-files` / `head` の各 `if not X.ok: return None, "<固有の理由>"`
は理由文字列がそれぞれ違い（S7 の指摘で意図的に分けたもの）、共通化すると
「どの git が落ちたか」が画面から消える。そのまま残した。

### 4. `service.stale_units` の逆引きを map に

`next((key for key, name in UNIT_NAMES.items() if name == unit), None)` を
モジュール定数 `UNIT_KEYS = {name: key for key, name in UNIT_NAMES.items()}` からの
`.get(unit)` に置き換えた。ループ内の generator 式が消え、`UNIT_NAMES` と対の関係で読める。

## 公開 IF 維持の確認

- 変更したのはすべて private（`_step` 新規 / `_refuse` `_new_report` `_upstream` の内部形）と
    モジュール定数 1 つ（`UNIT_KEYS` の追加。既存の名前は削っていない）
- 関数名・引数・戻り値型・エンドポイントのパスとステータスコード・pydantic の
    フィールド名はいずれも無変更
- 保護対象の契約テスト（`tests/web/selfupdate/`、`tests/test_update_sudoers_script.py`、
    `tests/test_web_service_script.py`）は **1 バイトも触っていない**
    （`git diff -- tests/web/selfupdate tests/test_update_sudoers_script.py` が空）
- テスト件数は 3218 / 110 のまま（減っていない）

## 同期したドキュメント

### docstring

| 対象 | 内容 |
| --- | --- |
| `repo.merge_fast_forward` | **変更不要**と判断。「git が自分で断った場合は 1 バイトも動かない／checkout 途中で打ち切られた場合はこの限りではない」は既に正確。ただし `_execute` の step 3 コメントが「失敗しても HEAD と作業ツリーは 1 バイトも動かない」と**無条件に**書いていたので「git が自分で断った場合は」に限定した（docstring と食い違っていた唯一の箇所） |
| `service.restart_permitted` | 「二重にかける」→ **なぜ片方では足りないか**を書いた。`COLUMNS` は sudo が tty を持たないときだけ見る env フォールバックなので本番では効くが tty のある手元実行では効かない、`_unwrapped` はその折り返しを sudo の折り返し方に依存せず畳む、片方だけだと「正しく設置した機体で『許可されていません』と嘘をつく」経路が残る |
| `runner._step`（新規） | 手順ごとに保存する理由（プロセスが死んでも止まった位置が残る）を明記 |
| `runner._refuse` | 状態も返す理由（git を 2 度起こさない／判定した HEAD と report の HEAD が一致する）を明記 |
| `repo._upstream`（新規） | 1 行サマリのみ |
| `steps.smoke_command` | **変更不要**。「本番は必ず `smoke_modules(active_units)` の結果を渡す」「既定を残しているのは契約テストが引数なしの形を固定しているため」が既に書かれており、読み手に伝わる |
| `service.RESTARTABLE_STATES` | **変更不要**。`failed` を含める理由（起動失敗したリビジョンを直しても更新が届かなくなるのを防ぐ／計画書「既知のリスク 1」からの復帰経路）が既にコメントにある |
| `tests/test_package.py::TestSelfUpdateImportLight` | docformatter が `ml-runtime` を `ml-\nruntime` に割っていたのを解消。文を組み替えて **再 format しても割れない**ことを確認した |

### README.md

`### WebUI からの更新` 節は最終実装と一致していることを確認した（env 一覧の既定値、
`*_UV_SYNC_ARGS` の「丸ごと置換」、復旧手順、`install-update-sudoers.sh install` と
`web-service.sh install <target>` の再実行、攻撃面の「LAN に居る者 ∪ GitLab に push できる者」、
merge タイムアウト 600 秒、`PCBASM_*_UPDATE_STATE_DIR` がリポジトリ直下固定であること）。
**追加したのは 1 点だけ**:

- `### systemd サービス` の例に `./scripts/web-service.sh render api` を追加し、
    「設置済み unit との差分確認用の読み取り専用サブコマンド（sudo も systemctl も不要）。
    WebUI の自己更新がこれを読んで『再 install が要る』を出す」を添えた。
    `render` は R5 対応で足されたが `usage()` にしか載っていなかった

## 簡素化できなかった部分・理由

- **`steps.smoke_command` の既定引数**は外せない（`tests/web/selfupdate/test_steps.py` が
    引数なし呼び出しを固定している = 保護対象）。plan-implementer の判断のまま
- **`tests/test_web_service_script.py:272` の docformatter による識別子分割**
    （`` `start-\nlimit-hit` ``）は残っている。**保護対象ファイルなので触れなかった**。
    契約テストの解禁後に 1 行直すだけで消える
- **`tests/web/update_support.py` と `tests/web/selfupdate/conftest.py` のスタブ重複**は
    そのまま。統合には保護対象ファイルの編集が要る（code-reviewer も「落として良いが
    負債として記録」と裁定済み）
- **`repo.capture_state` の 4 つの失敗検査**は上記のとおり意図的に個別のまま

## 気になったが触らなかった点（次の MR 候補）

1. `runner._execute` は依然として 70 行ある。ただし「7 手順を上から読む」構造そのものが
    価値なので、これ以上の分割（手順ごとのメソッド化）は追跡しづらくなるだけと判断した
2. `report.py` が `web.api.models` を import している（plan-implementer の逸脱 2）。
    import 契約は `tests/test_package.py` が守っているが、`web.selfupdate` →
    `web.api` の向きは長期的には逆転させたい（pydantic モデルを `selfupdate` 側へ移す）
3. `tests/web/test_update_host_shapes.py` がミラーレイアウト外。理由はファイル冒頭に
    書かれており、保護対象の制約由来なので今回は動かしていない

## 検証結果

| コマンド | 結果 |
| --- | --- |
| `make format` | pass（2 度目以降も no-op = docformatter が安定） |
| `uv run pyright src/web tests/web tests/e2e` | **0 errors, 0 warnings** |
| `uv run pytest --ignore=tests/ml -m "not hardware and not e2e" -q` | **3218 passed**（変更前と同数） |
| `uv run pytest --ignore=tests/ml -m "e2e and not hardware" -q --timeout=180` | **110 passed**（変更前と同数） |

`make test-no-hardware` / `make type` を直接使わないのはこの Pi に torch が無く
`tests/ml` が collect できないため（変更前から同じ）。実機テスト・`sudo`・実 systemd には
触れていない。commit もしていない。
