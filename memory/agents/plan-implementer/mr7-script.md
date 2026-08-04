# MR7 トラック A（`web-service.sh` と `Makefile`）

対象ブランチ: `chore/20260730/web-service-units`。契約は `/tmp/pcbasm-plan/mr7-brief.md` の §1 / §2。
コミットはしていない（orchestrator が行う）。

## 成果物

- `webui-service.sh` → `web-service.sh`（`git mv`。`git status` は `R` で検出）
- `Makefile`: `webui` / `webui-dev` / `webui-fake` を削除（`make help` は `## ` コメントから
  自動生成なので追加作業なし）
- `tests/test_web_service_script.py`（新規、24 ケース）
- `tests/test_makefile_fake_targets.py` に `TestWebuiAliasesAreGone` を追加

## 設計判断

| 判断 | 理由 |
| --- | --- |
| unit テキスト生成を `render_unit <target>` に切り出し、stdout に出すだけにした | systemd に触らずに全文をテストできる。install は `render_unit > tmpfile` を `install -m 0644` するだけ |
| `SYSTEMD_UNIT_DIR` / `SUDO` を env で上書き可能にした（既定 `/etc/systemd/system` / `sudo`） | テストの seam。`SUDO=echo` にすると「実行しようとしたコマンド」が stdout に出るだけで systemctl は動かない。実機の systemd を触らずに旧 unit 掃除の経路まで検証できる |
| 末尾の dispatch を `if [ "${BASH_SOURCE[0]}" = "${0}" ]` で囲み `main` に集約 | source して関数単体を呼べる（テストは `bash -c 'source web-service.sh; render_unit api'`）。sourced 時は `$0` が `bash` なので dispatch しない |
| `ExecStart=${make_bin} ${target}` と対象名を直結 | 対象名 / unit 名 / make ターゲット名を `api` `ui` で揃える設計。分けると 3 箇所の対応表になる |
| 対象の検証（`resolve_targets`）を `main` の先頭、特権コマンドより前に置いた | `./web-service.sh install bogus` が副作用ゼロで exit 2 する。テストもこの経路だけは実プロセス起動で確認している |
| `purge_legacy_unit` を `install_service` の先頭で呼ぶ（`all` では 2 回呼ばれるが 2 回目は no-op） | 旧 `pcbasm-webui.service` は `ExecStart=make webui` を参照しており、エイリアス廃止後に残ると restart ループに入る |
| 起動順の依存を付けない（`After=network-online.target` のみ） | `After=pcbasm-api.service` を付けると同居機で backend の起動失敗が frontend を止める。frontend は backend が落ちていても起動でき 503 を返すだけ。理由は `render_unit` 直上のコメントに残した |
| `status` だけ `require_privileged_tools`（非 root + sudo + systemctl）をスキップ | 元コードと同じ。status は sudo 不要 |
| `/tmp/pcbasm-webui-fake`（`api-fake` の `PCBASM_API_DATA_DIR` 既定）は**改名しなかった** | 契約 §2 は webui エイリアスの削除のみ。改名すると `.claude/skills/webui-e2e/SKILL.md` の記述と食い違い、そこはトラック B / `edit-dot-claude` 手順の担当。MR3 のメモでも「MR7 の整理対象」止まりだったので、やるなら別判断で |

## 検証

```
bash -n web-service.sh                                     # OK（shellcheck は未インストール）
./web-service.sh --help                                    # usage 目視
SUDO=echo bash -c 'source ./web-service.sh; render_unit api; render_unit ui'  # unit 全文を目視
uv run pytest tests/test_makefile_fake_targets.py tests/test_web_service_script.py -m "not hardware" -q  # 34 passed
uv run pyright tests/test_web_service_script.py tests/test_makefile_fake_targets.py                      # 0 errors
uv run pre-commit run --files web-service.sh Makefile tests/test_web_service_script.py tests/test_makefile_fake_targets.py  # 2 回目で無変更
```

`systemctl` は一度も実行していない（本番 `pcbasm-webui.service` は稼働中のまま）。
ツリー全体の `make format` / `make type` / `make test-*` はトラック B と同時進行のため実行していない。

## mutation（一時コピーに当てて全て kill / driver は scratchpad の `mr7_mutate.py`）

| mutation | 落ちたテスト |
| --- | --- |
| `ExecStart=... make webui` に戻す | `TestRenderUnit::test_execstart_runs_the_make_target_of_the_same_name` |
| `After=pcbasm-api.service` を足す | `TestRenderUnit::test_no_ordering_dependency_between_the_two_services` |
| `DEFAULT_TARGET="all"` | `TestTargetResolution::test_resolves[-api]` |
| `install_service` から `purge_legacy_unit` を消す | `TestLegacyUnitPurge::test_install_purges_the_legacy_unit` |
| Makefile に `webui: api` を復活 | `TestWebuiAliasesAreGone::test_makefile_defines_no_webui_target` |

baseline（無変更コピー）は 34 passed。

## 引き継ぎ

- `git log --follow web-service.sh` は**コミット後に**確認できる（現時点では rename が staged なだけ）。
  `git diff --cached -M --stat` は `webui-service.sh => web-service.sh` を検出済み。
- README の `./webui-service.sh …` 6 行（契約 §3）はトラック B の担当。新コマンド構文は
  `./web-service.sh install|start|stop|restart|status|remove [api|ui|all]`（既定 `api`）。
- 実機での置き換え（`./web-service.sh install all` 等）はユーザーが行う。

## レビュー差し戻し対応（トラック A / A1-A6、2 巡目）

code-reviewer の should-fix（A1-A6）を反映。所有ファイルは `web-service.sh` / `Makefile` /
`tests/test_web_service_script.py` / `tests/test_makefile_fake_targets.py` のみ。コミットはしていない。

### A1 `status all` が 1 本目で止まる

`main` の for ループで `show_status` の非 0（inactive=3 / 未登録=4）が `set -e` を踏んで
スクリプトごと終了し、2 本目（`pcbasm-ui.service`）が表示されなかった。片方だけ落ちた同居機の
切り分けに使う経路なので致命的。

- 修正: ループ内で `rc=0; show_status "${target}" || rc=$?` として打ち切らず、`status_rc` に
  **最初の非 0 をそのまま保持**して `main` の末尾で `return "${status_rc}"`。
- 終了コードの契約: 「全対象を見た結果」。1 つでも不健全なら非 0（inactive なら 3 が出る）。
  systemctl の値をそのまま返すので `all` で 2 つ落ちているときは先に見た api の値になる。
  集約して 1 に潰すより、単体実行と同じ値が返るほうが診断に使いやすいと判断した。
- `show_status` は失敗しても抜けさせない（呼び出し側が回しきる）ことをコメントで明示。

### A2 `start|stop|restart all` が 1 本目の未登録で全体終了

`control_service` に第 3 引数 `on_missing` を足した。**`all`（複数対象）は `skip`、単体指定は
`error`** を選んだ。理由（コード上のコメントにも残した）:

- `all` で未登録は「機体は api のみ / frontend 機は ui のみ」という**正常な構成**でも起きる。
  同じ `all` ループ内の `remove_service` が未登録を `return 0` で読み飛ばすのと対称にした。
- 単体指定（`start api`）で未登録は対象を明示した上での install 忘れ / 打ち間違いなので、
  従来どおり `exit 1`。ここを緩めると「起動したつもりで起動していない」に気付けない。
- `all` のスキップは exit 0（警告は stderr）。remove と揃えた。片方が起動できたのに全体を
  非 0 にすると呼び出し側スクリプトの分岐が増えるだけで得がない。
- 対象数の判定は `read -r -a target_list <<<"${targets}"` で配列化し `${#target_list[@]} -gt 1`。
  ループも `for target in "${target_list[@]}"` に変えた（未クォート展開の除去も兼ねる）。

### A3 `install ui` が稼働中の backend を消す

`purge_legacy_unit` を `install_service` の先頭から**対象が `api` のときだけ**に移した。
旧 `pcbasm-webui.service` は backend そのものなので、`install ui` で消すと同居機の backend が
消えて frontend だけが残る。`install ui` 時は `warn_legacy_unit` で
「旧 unit が残っています。`install api` を実行してください」を stderr に出すだけにした。
`install all` は api → ui の順なので api 側で掃除され、ui 側の警告は no-op になる。

### A4 `remove` が旧 unit を残す

`remove_service` の**先頭**（新 unit の存在チェックより前）で、対象が `api` のときだけ
`purge_legacy_unit` を呼ぶ。未移行の機体では新 unit が無く「登録されていません」で抜けるため、
順序が逆だと enabled な旧 unit が残る。`all` は api を含むので契約どおり掃除される。
`remove ui` では触らない（backend を消さない）。

### A5 実装テキストを見るテストを振る舞いテストへ

`TestLegacyUnitPurge::test_install_purges_the_legacy_unit`（`declare -f install_service` の
関数ソース文字列を grep）を削除し、`SUDO=echo` + `SYSTEMD_UNIT_DIR=<tmp>` の既存 seam で
振る舞いをテストする `TestInstall` / `TestRemove` に置き換えた。

- `test_rendered_unit_is_installed_at_the_unit_path[api|ui]` — `install -m 0644 <tmp> <unit_path>`
  と `systemctl enable pcbasm-<target>.service`（従来未カバー）
- `test_install_api_purges_the_legacy_unit` — **単体 `install api`**（最多数の運用、従来未カバー）
- `test_install_ui_keeps_the_legacy_unit_and_warns` — A3 の回帰
- `TestRemove` 2 件 — A4 の回帰と `remove ui` が旧 unit を残さないこと

`main` 経路（`status all` / `start all`）は実プロセス起動でしか観測できないため、`run_script` に
`stub_bin` 引数を足し、`stub_systemctl()` が **PATH 先頭に `systemctl` スタブ**を置く
（引数を echo し、指定 unit を含むときだけ exit 3）。実機の systemd は一度も呼ばない。

### A6 `make help` が `test-e2e` を出さない

help の grep が `'^[.a-zA-Z_-]+:.*?## '` で**数字を含まない**ため `test-e2e` が一覧から落ちていた。
文字クラスに `0-9` を足した（`'^[.0-9a-zA-Z_-]+:.*?## '`）。**MR7 で README が `make test-e2e` を
案内するようになったため、そのドキュメント整合として直した既存の不具合**（MR7 の変更が原因では
ない）。コミットメッセージにはこの理由を書く。テストは `make -f <Makefile> help` を実運転し、
ANSI 付き出力からターゲット名を拾って `test-e2e` の存在を assert する（grep + awk だけなので副作用なし）。

### usage の同期

`install` / `remove` の説明から「旧 unit は削除する」を外し、
「旧 unit の削除は対象が api または all のときだけ（ui 単体では警告のみ）」を独立行にした。
`status` に「all では全対象を表示する」を追記。

### 検証

```
bash -n web-service.sh                                                       # OK
uv run pytest tests/test_web_service_script.py tests/test_makefile_fake_targets.py -m "not hardware" -q  # 49 passed
uv run pyright tests/test_web_service_script.py tests/test_makefile_fake_targets.py                      # 0 errors
uv run pre-commit run --files web-service.sh Makefile tests/test_web_service_script.py tests/test_makefile_fake_targets.py  # 2 回目で無変更
bash <scratchpad>/manual_check.sh   # install all / status all / restart all / restart api / remove all / remove ui を seam 越しに目視
```

`systemctl` は一度も実行していない（PATH 先頭スタブ + `SUDO=echo`）。ツリー全体の
`make format` / `make type` / `make test-*` は並列作業中のため実行していない（合流検証は後段担当）。

`manual_check.sh` の目視結果（要点）:

- `install all` → 旧 unit を disable + rm したうえで api → ui を登録（ui 側は警告のみ）
- `status all` → api（stub が exit 3）と ui の**両方**を表示して `rc=3`
- `restart all`（両方未登録）→ 警告 2 件で `rc=0`／`restart api`（未登録）→ エラーで `rc=1`
- `remove all` → 旧 unit を撤去してから両対象を処理

### mutation（一時コピーに当てて全て kill / driver は scratchpad の `mr7a_mutate.py`）

| mutation | 落ちたテスト |
| --- | --- |
| A1: `status` を `show_status "${target}"` 直呼びに戻す | `TestStatusAll::test_shows_every_target_even_when_the_first_is_unhealthy` |
| A2: `on_missing` 分岐を消して常に `exit 1` | `TestControlMissingUnit::test_all_skips_the_unregistered_target_and_continues[start/stop/restart]` |
| A3: `purge_legacy_unit` を対象に関係なく呼ぶ | `TestInstall::test_install_ui_keeps_the_legacy_unit_and_warns` |
| A4: `remove_service` の掃除を削除 | `TestRemove::test_remove_api_purges_the_legacy_unit_on_an_unmigrated_machine` |
| A5: `install_service` の掃除/警告ブロックを削除 | `TestInstall::test_install_api_purges_the_legacy_unit`, `test_install_ui_keeps_the_legacy_unit_and_warns` |
| A6: help の文字クラスから `0-9` を消す | `TestHelpListsEveryDocumentedTarget::test_help_lists[test-e2e]` |

baseline（無変更コピー）は 49 passed。

### 確認事項（orchestrator 経由）

- `status all` の終了コードを「最初の非 0 をそのまま返す」にした（例: api が inactive なら 3）。
  監視スクリプトが `0/1` を期待するなら 1 に潰す形へ変える。現状の指示範囲では systemctl の値を
  透過するほうが情報量が多いと判断した。
- `start all` で両方未登録のとき exit 0（警告のみ）。`remove` との対称性を優先した結果で、
  「何も起動していないのに成功」を嫌うなら非 0 にする余地がある。

## 最終仕上げ（F1-F8、3 巡目）

code-reviewer の must-fix / should-fix / 採用した nit（F1-F8）を反映。コミットはしていない。

### F1 README が旧仕様のまま（must-fix）

`README.md` の移行手順が「`install` は旧 `pcbasm-webui.service` を見つけたら disable して削除する」と
**無条件に**書いており、2 巡目の A3（`install ui` は削除せず警告のみ）と食い違っていた。
スクリプトの usage だけ正しく、運用者が読む README が旧仕様という状態。

- 旧 unit の掃除は**対象が `api` または `all` のときだけ**行うことを明記
- `install api` / `install all` / **`remove api` / `remove all`** が掃除することを列挙
  （`remove` 側が掃除することは従来 README に一切書かれていなかった）
- `install ui` は削除せず警告するだけ、`remove ui` は旧 unit に触らないことを明記
- 運用の対応（**同居機 = `install all` / 機体 = `install api` / frontend 専用機 = `install ui`**）を
  1 文で明示。コードブロックのコメントも「frontend 専用機」に揃えた

**usage 側も 1 行直した**: `web-service.sh:33-34` の「（ui 単体では削除せず警告のみ）」は
`remove ui` まで警告するように読めるが、`remove_service` は `warn_legacy_unit` を呼ばない。
「ui 単体では削除せず、install ui は残っていれば警告するだけ」に精密化した
（挙動は変えていない。`remove ui` に警告を足すのは要求外なのでやらない）。

### F2 `install` / `remove` が `main` 経路で未テスト（should-fix）

`main` の `install)` / `remove)` 分岐本体を `echo BROKEN-*` に置き換えても緑だった。
`TestMainDispatch` を新設し、**プロセス起動（`run_script`）で成功パス**を固定した。

- `test_install_writes_and_enables_the_unit[install / install api]` — 対象省略が `api` と同じ
- `test_install_all_registers_both_targets` — 2 unit の内容 + enable + restart
- `test_install_all_purges_the_legacy_unit_once` — 同居機の移行（旧 unit が消え、ui 側の警告も出ない）
- `test_remove_deletes_the_installed_unit` / `test_remove_all_deletes_both_units_and_the_legacy_one`

### F3 `daemon-reload` / `restart` が未固定（should-fix）

- `test_install_restarts_the_service[api|ui]` — restart 欠落は「unit は設置・enable されたのに
  新 backend が起動しない」= この MR が解消しようとしている移行失敗そのもの
- `test_daemon_reload_precedes_enable` — 存在だけでなく **`enable` より前**を assert する
  （後ろにあると `enable` が古い unit 定義を掴む）。stub の echo 行の index 比較で見ている

### F4 `warn_legacy_unit` の否定側が未テスト（should-fix）

`test_install_ui_does_not_warn_without_the_legacy_unit` を追加。旧 unit を一度も持ったことが
ない frontend 専用機で `install ui` する度に誤警告が出る回帰を検出する。存在チェックを外す
mutation はこれと `test_install_all_purges_the_legacy_unit_once` の 2 本で kill される。

### F5 `require_privileged_tools` の呼び出しが未テスト（nit → 採用）

`TestRequiredTools::test_install_fails_clearly_when_systemctl_is_missing`。PATH を
「`sudo` スタブ + `dirname` だけ」に置き換え（`PROJECT_ROOT` の算出に `dirname` が要る）、
`systemctl` が無い状態で `install api` が非 0 + stderr にコマンド名を出して終わり、
unit ディレクトリに何も書かないことを確認する。

**受容した未検証範囲**: `require_non_root`（`EUID -eq 0`）自体はテストしていない。root で
pytest を走らせないと再現できず、そのために特権テストを持ち込む価値はないと判断した。
`main` からの `require_privileged_tools` 呼び出しの消失は上記テストで検出できる。

### F6 seam が弱く assert が空振り（should-fix / 重要）

`SUDO=echo` では `rm` も `install` も**実行されない**ため、
`test_install_ui_keeps_the_legacy_unit_and_warns` の `legacy.exists()` は
「何も削除されないから当然通る」空振り assert だった。

`stub_sudo()` を追加した。`exec "$@"` するだけの no-op ラッパを **`sudo` という名前で**
PATH 先頭に置き、`SUDO` にはその絶対パスを渡す（`require_command sudo` もこれで満たされ、
実機の `sudo` は一度も呼ばれない）。`systemctl` は従来どおり PATH 先頭スタブが受ける。
`SYSTEMD_UNIT_DIR` は tmp なので **systemd には一切触れない**まま、`install` / `rm` が実際に走る。

これで purge / install の assert を**ファイルシステム上の実結果**に置き換えた:

- `not legacy.exists()` / `legacy.exists()`（echo された文字列ではなく実削除を見る）
- `installed.read_text() == render_unit(target, ...)` と `mode == 0o644`（設置内容とパーミッション）
- `remove` 後に unit ファイルが消えていること

`SUDO=echo` は「コマンド列だけ見る」用途として残し、**どちらの seam を何に使うかを
モジュール docstring に明記**した（同じ空振りを次に書かせないため）。

### F7 EXIT trap がテンポラリを掃除できていない（nit → 採用）

`install_service` の `trap 'rm -f "${unit_file:-}"' EXIT` は、trap 本文が**関数フレームが
巻き戻された後に評価される**ため `local` の `unit_file` が空になり `rm -f ""` になっていた。
特権 install が失敗すると `mktemp` したファイルが `/tmp` に残る。設置時に展開する
`trap "rm -f '${unit_file}'" EXIT`（+ `# shellcheck disable=SC2064` と理由コメント）に直した。

**これは旧 `webui-service.sh` から引き継いだ既存のバグで、MR7 が持ち込んだものではない。**
ただし MR7 でこの関数を大きく作り替えた以上、読み手は検証済みだと解釈するため、
**同じ関数を作り替えたついでに直した**（コミットメッセージにこの理由を書く）。
`test_temporary_unit_file_is_removed_when_the_privileged_install_fails` で固定した
（`TMPDIR` を tmp に向け、`SUDO=false` で特権 install を失敗させ、残骸が無いことを見る）。

### F8 実在しないテストファイルへの参照（nit → 採用）

`tests/web/api/routers/test_preview.py` のモジュール docstring が実在しない
`tests/e2e/test_webui_e2e.py::TestPreviewOverRealHttp` を参照していた
（当該クラスの実体は `tests/e2e/test_api_e2e.py:122`）。1 行修正。
**直前の巡回で `.agents/skills/` の同種の宙ぶらりん参照を 3 本直したので、同じ範囲として直した**
（コミットメッセージに「宙ぶらりん参照の掃除」としてまとめて書ける）。

`memory/agents/**` に残る `test_webui_e2e.py` 参照は**過去の作業記録なので直さない**
（当時の事実を書いた履歴であり、現状の案内ではない）。

### 却下した提案（記録のみ）

| 提案 | 裁定 | 理由 |
| --- | --- | --- |
| 実装ノートのファイル名を契約の `mr7-web-service-units.md` に統一 | 却下 | トラック別（`mr7-script.md` / `mr7-docs.md`）の方が読みやすい。MR6 でも同じ裁定 |
| `make help` を 4 回起動する parametrize を fixture に畳む | 却下 | 副作用が無く実行時間も短い。好みの範囲 |

### 検証

```
bash -n web-service.sh                                    # OK
make format（2 回）                                        # 1 回目で ruff-format / docformatter が整形、2 回目は無変更
make type                                                 # 0 errors, 0 warnings
make test-no-hardware                                     # 2170 passed, 124 deselected
make test-e2e                                              # 88 passed, 2206 deselected
```

`systemctl` は一度も実行していない（PATH 先頭スタブ + `SUDO` no-op ラッパ + `SYSTEMD_UNIT_DIR`=tmp）。
`make test` / `make run` は実行していない。`api-fake` / `ui-fake` の `*_DISCOVERY_ENABLED=0` は無変更。

### mutation（一時的に `web-service.sh` を書き換え → 実行 → 必ず復元。driver は scratchpad の `mr7_final_mutate.py`）

| mutation | 落ちたテスト |
| --- | --- |
| F2: `main` の `install)` 分岐 → `echo BROKEN-INSTALL` | `TestMainDispatch::test_install_writes_and_enables_the_unit[×2]`, `test_install_all_registers_both_targets`, `test_install_all_purges_the_legacy_unit_once` |
| F2: `main` の `remove)` 分岐 → `echo BROKEN-REMOVE` | `TestMainDispatch::test_remove_deletes_the_installed_unit`, `test_remove_all_deletes_both_units_and_the_legacy_one` |
| F3: `systemctl restart` の行を削除 | `TestInstall::test_install_restarts_the_service[api / ui]`, `TestMainDispatch::test_install_writes_and_enables_the_unit[×2]`, `test_install_all_registers_both_targets` |
| F3: `systemctl daemon-reload` の行を削除 | `TestInstall::test_daemon_reload_precedes_enable` |
| F4: `warn_legacy_unit` の存在チェックを外す | `TestInstall::test_install_ui_does_not_warn_without_the_legacy_unit`, `TestMainDispatch::test_install_all_purges_the_legacy_unit_once` |
| F5: `main` から `require_privileged_tools` の呼び出しを削除 | `TestRequiredTools::test_install_fails_clearly_when_systemctl_is_missing` |
| F6: `purge_legacy_unit` の `rm` を削除 | `TestInstall::test_install_api_purges_the_legacy_unit`, `TestRemove::test_remove_api_purges_the_legacy_unit_on_an_unmigrated_machine`, `TestLegacyUnitPurge::test_purge_disables_and_deletes_the_legacy_unit`, `TestMainDispatch::test_install_all_purges_the_legacy_unit_once`, `test_remove_all_deletes_both_units_and_the_legacy_one` |
| F7: EXIT trap を遅延展開（旧実装）に戻す | `TestInstall::test_temporary_unit_file_is_removed_when_the_privileged_install_fails` |

**全 8 mutation が kill された**（F6 は seam 強化前は survive していたもの）。baseline は
`tests/test_web_service_script.py` 単体で 49 passed。復元後の `web-service.sh` は
mutation 前とバイト一致（driver が `assert` で確認）。壊した状態は残していない。
