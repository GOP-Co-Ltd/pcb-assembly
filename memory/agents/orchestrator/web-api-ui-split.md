---
description: WebUI を web.api / web.ui へ分離する計画（MR !153）の実装スタック — 委譲判断・レビュー裁定・計画外の決定
---

# web.api / web.ui 分離の実装統括

計画書は MR !153 のブランチ `docs/20260730/web-api-ui-split` の
`docs/plans/web-api-ui-split.md`。実装は MR0（安全対策）を根に置いた直列スタック。

## MR スタックの構成

```
main
 └─ !156  chore/20260730/block-hardware-test-execution   実機テスト実行の機構的禁止
     └─ MR1  fix/20260730/board-settings-rmw
         └─ MR2 → MR3 → MR4 → MR5 → MR6 → MR7
```

**完全な直列にした理由。** 計画書の依存グラフは MR1 と MR2 を並列可としているが、
両者は `state.py` を触る（MR1: `_persist_lock` / `merge_job_param_defaults`、
MR2: `AppState.nozzle_cap()`）。かつ MR3 は MR1 と MR2 の**両方**に依存するため、
MR2 を main target にすると MR3 の diff に MR1 が混入してレビュー不能になる。
直列なら各 MR の diff が自分の変更だけになり、MR1 から順にマージすれば GitLab が
target を自動追従する。

**MR0 をスタックの根にした理由。** `.claude/hooks/` の実機テスト禁止フックを根に
置くと、以降のすべての作業ブランチでフックが有効になる。各 MR の diff も汚れない。

## 実機テスト事故（2026-07-30）

MR1 のレビュー段階で、反証エージェントが
`timeout 900 uv run pytest tests/webui -q -x --timeout=120` を起動した。
`tests/webui/` には `@mark_hardware` が含まれる（`test_posctrl.py:388`、
`test_machine_control.py`、`test_nozzle_cap.py`、`test_system.py`）ため、
これは実機テストの実行と等価。3 分 35 秒後に検知して停止した。

**物理的な影響なし**を次で確認した: `homed_axes` が空（一度もホーミングされて
いない）、`print_time` が klippy.log 末尾 400 行すべてで `5666.611` のまま不変
（gcode が 1 行も実行されていない）、`idle_timeout` が `Ready` /
`printing_time 0.0`、`print_stats` が `standby`。

**原因はブリーフの不備。** 「`make test` と `@mark_hardware` を実行しない」と
書いたが、「実機テストを含むディレクトリを marker なしで指定する」経路を
塞げていなかった。→ MR0 でフックを追加し、以降のブリーフには
「pytest には必ず `-m "not hardware"` を付ける」を明記する。

## MR1 のレビュー裁定

3 視点（計画準拠 / 並行性 / テスト品質と既存回帰）で 28 件の指摘。全件を
反証者が独立に検証し、22 件が反証された。生存した 6 件の事実関係はすべて実測で
裏付けられている。

### 採用（must-fix）

| # | 指摘 | 採用理由 |
| - | ---- | -------- |
| 1 | `JobManager` と `app.state.board_store` の同一性テストが無い（3 名が独立に指摘） | 計画書が「これをやらないとロックが効かない」と明記した唯一の不変条件。`isinstance` だけでは自前生成に戻す回帰が全緑で通る。修正は public な `ctx.board_store` との同一性 assert 1 行 |
| 2 | `_update_lock` の回帰ガードが弱い（決定的テストは検出力ゼロ、並行テストは 3〜7/10） | MR1 の中心機構。`mutate` 内で `Event` を待たせて直列化を決定的に観測する。誤解を招くテスト名（`inside_lock` だが再 load しか見ていない）も直す |
| 3 | `test_concurrent_merges_..._lose_nothing` が `_persist_lock` の回帰を検出しない（no-op 化で 5/5 緑） | 「守っているつもりで守っていないテスト」は無いより悪い。名前を実態に合わせ、別 job 名 2 本で `_persist` の dumps 走査中の挿入を突く形に強化する |
| 4 | 並行テストがスレッド内例外と未終了を検証せず false green（merge を RuntimeError にしても緑） | MR1 の証拠となるテスト自身が false green。例外回収 + `assert not thread.is_alive()` の数行 |
| 5 | initial-purge の 409 で machine.toml が不変であることの assert が無い | machine.toml は基板横断のグローバル設定。3 経路のうち board JSON 以外へ副作用を持つのはここだけ |
| 6 | `test_rejected_patch_does_not_persist` の第 1 assert がディレクトリ不在でも通る（空虚） | 「書かれなかった」と「一度も書かれていない」を区別できていない |

### 採用（should-fix）

7. `BoardSettingsStore` クラス docstring を実態に合わせる — `prune` / import は
   `_update_lock` 外の全量上書きであり、同時編集との原子性は保証しない旨を明記。
8. 既知の制約をノートに記録 — prune / import のロック外書き込み、
   `board_signature` の巻き戻り、`write_text_atomic` に fsync が無いこと。

### 却下

| 指摘 | 却下理由 |
| ---- | -------- |
| `prune` / `save` を `_update_lock` 配下に入れる（2 名が should-fix/high で指摘） | import が書くのは**アップロードされた doc から復元したモデル**で、保存済み JSON を読んでいない（`load_board` の戻り値は `base_config` / `hierarchy` / `source_pcb` / signature の供給のみ）。したがって `save` の docstring が禁じる read-modify-write ではなく「全量上書き」。計画書も変更対象を 3 PATCH に限定し import/export/prune を対象外と明記。修正には `save` のロック化＝再入回避リファクタが伴い外科的範囲を超える。**別 MR 候補として記録**し、docstring に例外を明記する（採用 7） |
| `write_text_atomic` に fsync を追加 | 計画書の指示は「同一ディレクトリ `NamedTemporaryFile` → `Path.replace`」で fsync を含まない。本 diff は `config_store` が元から持っていた同処理を関数へ寄せただけで、クラッシュ耐性は変更前後で同一。投機的な防御追加 |
| `webui_state.json` の permission 0644 → 0600 を維持 | 前提が実機で偽。`pcbasm-webui.service` は `User=gop`（手動 `make webui` と同一）、`config/machine.toml` と `board_settings/*.json` は本 diff 前から同経路で 0600。より重要な machine.toml が 0600 で運用できている |
| `job_param_defaults` の読み取りロックを外す | `_persist` はロック保持中に外側 dict を `json.dumps` で走査するため読み手側ロックにも根拠がある。「計画外だから外せ」は、それ自体が要求外の変更 |
| `expected_pcb` の 409 判定を `load_board` の前へ前倒し | canonical な `loaded.source_pcb` と比較する現設計が正しい。前倒しは `source_pcb` 導出の重複と「PCB 未選択 409」との順序変更を招く。稀な 409 経路のパース 1 回のためだけの要求外の最適化 |
| 同一ノード・別フィールド同時編集のテストを board_settings に追加 | マージ semantics は pcbasm 層の責務で `tests/pcbasm/pasting/test_settings.py::TestWithLevelPatch` に既にテストがある。層の重複 |
| `board_signature` の巻き戻り（S1 → S2 → S1） | 直すには signature をロック内で再計算＝PcbFile パースと階層構築をロック内に持ち込むことになり、計画書の「PCB のパース・階層構築・入力検証はロック外」指示に正面から反する。`save` 版でも同じ挙動＝MR1 由来ではない。**残課題として記録**（採用 8） |
| `test_missing_parent_directory_raises` の削除 | stdlib の追試ではなく `write_text_atomic` docstring 前提（親ディレクトリは存在している必要がある）の契約ピン |
| `manager.py` の既定値保存が replace → merge に変わった | `validate_params` が default を持つキーを常に充填するため現在の `persisted_params` では同値。実装者ノートに理由込みで記録済み |

## MR1 の 2 巡目（修正 + 検出力の測定）

採用 8 件を修正させたうえで、**実装を影コピーで壊して各テストが本当に落ちるか**を
独立に測らせた（mutation testing）。1 巡目で見つかった欠陥が「守っているつもりで
守っていないテスト」だったため、同じ失敗を繰り返さないための工程。

### 実装バグ 1 件が 2 巡目で出た

`merge_job_param_defaults` が **`_persist_lock` を取っていなかった**。docstring と
計画書は「ロック内で読む → マージ → 永続化」を要求しており、明確な実装漏れ。

これを **1 巡目のレビュー 3 名と反証者 1 名の誰も指摘しなかった**。反証者はむしろ
「`job_param_defaults` の読み取りロックにも根拠がある（`_persist` が外側 dict を
走査するため）」と書いており、**書き手がロックを取っていない**という前提の崩れに
気づいていない。読む方向のレビューだけでは、こういう「あるべきものが無い」欠陥は
落ちる。**実装を壊して落ちるかを測る工程が、レビューの穴を埋めた。**

### 私の指示が 1 点だけ事実と違った

項目 3 で「別 job 名の同時 merge 中に `_persist` の `json.dumps` が外側 dict を
走査 → `RuntimeError: dictionary changed size during iteration` を突け」と指示したが、
**Python 3.13 では到達不能**。`iterencode` の分岐が
`if _one_shot and c_make_encoder is not None:` で `indent is None` の条件が無く、
`json.dumps(..., indent=2)` が C エンコーダを通るため dict 走査中に GIL を解放しない
（実測で確認: python 3.13.5）。

実装者は勝手に諦めず、同じ `_persist_lock` が守る別の観測可能な性質
（A が dumps → B が dumps して replace → A が古い snapshot で replace = **ファイルの
巻き戻り**）を突く形に置き換えた。ロック no-op で 5/5 失敗・ロック有りで 10/10 pass
の決定的テスト。**代替を承認**した。

### 検出力の測定結果

11 種の壊し方を当てて全テストを測り、**vacuous なテストは 1 件も残っていない**。
リポジトリの `src/` `tests/` は一切変更されていない（影コピー + `PYTHONPATH` 方式）。

決定的（10/10 で落ちる）ものが大半だが、
`test_concurrent_updates_of_distinct_nodes_both_survive` だけは **13/20 = 約 65%**。
`_update_lock` の排他は `test_second_update_blocks_until_first_mutate_returns` が
10/10 で守るので穴は無いが、確率的である旨を docstring に明記させた
（緑であることを lost update が無い根拠にしない）。**誤った安心を与えるテストを
残さない**という 1 巡目の裁定基準を、2 巡目の自分の成果物にも適用した。

## MR2 のレビュー裁定

3 視点で 33 件 + 検出力測定。反証で 17 件が却下、16 件が生存。**同一事実を 3 名が独立に
指摘したものが 4 組**あり、いずれも実測で裏付けられている。

### ユーザー裁定を仰いだ 1 件

`mainsail_url` の代替値。計画書は「`request.url.hostname` フォールバックを削除」までしか
決めておらず（プロキシ配下では frontend のホスト名になるため削除は正しい）、代替値を
指定していない。実装は bare hostname（`http://kurousagi002`）にしていたが、**実機は
`PCBASM_MAINSAIL_URL` を unit にも Makefile にも設定していない**（実測）ため、これが
そのまま本番値になる。ラップトップからは mDNS が `*.local` しか引かないので、従来
（`http://kurousagi002.local`）から**リモートで開けなくなる回帰**。

→ ユーザー裁定で **`http://{machine_id}.local`** を採用。MR5 で avahi/mDNS が前提に
なるのとも整合する。

### 採用（must-fix）

| # | 指摘 | 採用理由 |
| - | ---- | -------- |
| M1 | `mainsail_url` の代替値 | 上記のユーザー裁定。`machine_id` が既にドットを含む場合は付けない |
| M2 | `PCBASM_WEBUI_PCB_ROOT` を設定すると `pcb_browse_allowed` が追従せずファイルブラウザが全パス 400（3 名が独立に実測） | `from_env` の docstring が公開仕様として書き `test_env_overrides_each_field` がピンしている env を無音で壊す。「投機的実装を避けた」では済まない — 既存の公開 knob と新しいセキュリティ境界が矛盾している |
| M3 | `TestPcbBrowseAllowed` が prefix 兄弟ディレクトリを検証していない | 検出力測定が**実際に脱出を実証**した。`_is_allowed` を `startswith` 実装にすると `/tmp/X/pcb-evil/secret.kicad_pcb` が列挙され選択できるのに、6 本すべてが緑。セキュリティ境界のテストが境界を留めていないのは、MR1 で立てた「誤った安心を与えるテストを残さない」に正面から反する |
| M4 | `move_to_cap` が生の `state.machine().nozzle_cap` を読み、部分記録で 400 ではなく 500 | `ClassValidationError` は `ExceptionGroup` 派生で `ValueError` ではないため `except ValueError` を通り抜ける。MR2 の目的そのものの取り残しで、`pages.py` は既に移してある。1 行 |

### 採用（should-fix）

| # | 指摘 | 裁定 |
| - | ---- | ---- |
| S1 | 部分記録 nozzle_cap の全 23 ページ parametrize は 1 件しか検出力が無い（3 名 + 測定が一致） | **削除ではなく転用**する。検出力測定が「壊れた machine.toml を fixture にすれば 23 本すべてに検出力が出る」ことを実証した（`_base_context` が呼ぶ `machine_name()` / `machine_type()` / `focus_z()` の防御は全ページに効く）。現在 `machine_name()` の防御は**未ピン**なので、そこを埋める形に作り替える。nozzle_cap は実際に読む 2 経路に絞り、`state.py` と test の docstring の事実誤認（「全ページの SSR に載る」）も直す |
| S2 | `SECTION_LABELS["machine_name"]` に回帰テストが無い | 計画書が理由付きで明示要求した 1 行。消すと `/settings` の見出しが生キー `machine_name` に退行するのに全緑（測定で実証） |
| S3 | `settings.py` のコメントが実装と矛盾 | 「OS 全体を閲覧可能にする」は同じコミットで偽になった。`pcb_upload_dir` の allowed 制約も未記載 |
| S4 | `test_pages.py` の `value="{default}"` assert が弱い | `loading_default` の注入ブロックを丸ごと削除しても全緑（測定で実証）。同じ既定値の別 input が同一ページに 2〜4 箇所あるため |
| S5 | `TestJobDefinitionDrivenContext` の loading テストが 2 feature で定数 True | `loading.html` / `dispense_calibration.html` が `loading_controls.html` を無条件 include するため、左辺が常に True |

nit 採用: `StateResponse.nozzle_cap` の `= None` を外す（OpenAPI の required に入る。計画書も
既定値なし）/ 撤去済み `_PASTING_PREVIEW` 等を指すコメント 2 行 /
`test_detects_a_forbidden_import` の削除（本体に vacuity ガードあり）/ docformatter の
折り返しで日本語に残った半角スペース 2 箇所。

### 却下

| 指摘 | 却下理由 |
| ---- | -------- |
| `StateResponse.mainsail_url` も解決済み値に揃える（3 名が指摘） | 計画書は `StateResponse` への追加を `nozzle_cap` のみと指定。`mainsail_url` を読むのは SSR テンプレート 2 箇所だけで JS は参照 0 件（grep）= 壊れる入力が無い。既存 API フィールドの意味変更は MR2 の範囲外。**MR4 で frontend が触るときの残課題として記録** |
| `machine_name = 42` で `/settings` が壊れる | 反証者の実測で 500 ではなく **400**（既存の全 `MACHINE_FIELDS` と同一挙動）。`test_config.py` の docstring の主張（表示名が壊れてもページを落とさない）は `/posctrl` 200・`/api/machine-info` 200 で実際に成立している |
| `build_machine_info` が machine.toml を 2 回パース（3 名が指摘） | 計画書が `Machine()` 3.8ms を明示的に許容したうえで「`/api/health` は作らない」と判断している。実測差 0.4ms。要求外の最適化 |
| symlink 絡み 2 件（許可 root が symlink だと列挙に出ない） | 実測で `/media` `/mnt` `/home` はすべて実ディレクトリ。`pcb_browse_allowed` は本 MR で新設で env 上書きも無く、symlink を書いた設定が存在しない。traversal / symlink escape は `_is_allowed` が resolve 後に判定するので塞がれている。CLAUDE.md「起こり得ないシナリオに対するエラーハンドリングは書かない」 |
| `_validate_pcb` に allowed 判定を足す | 計画書の検証点（`files.py` の resolve・アップロード先・`GET /api/files` の列挙）に state 復元は含まれない。要求外のハードニング。**残課題として記録** |
| `pcb_upload_dir` の root 配下検査 | 本 diff 前とまったく同じ挙動で、トリガは M2 と同じ env 誤設定 1 つ。M2 を直せば消える |
| `JobSpecInfo.loading_stages` の既定値二重定義 | `models.py` は pydantic のみ import という計画書の制約があるため `JobDefinition` の既定値を参照できない構造上の帰結。他フィールドも同様に複製している |
| `TestJobDefinitionDrivenContext` が定義側退行に同語反復 | 検出力測定が「既存 `TestPastingJobPages` の 3 本と `/api/jobs` 側の 1 本が落として検出する。役割分担として成立」と結論 |

## MR2 の 2〜3 巡目と、裁定の誤り 2 件

修正 → 検出力の再測定 → 検出力ゼロの作り直し、で 3 巡した。**私の裁定に事実誤認が 2 件
あり、どちらも mutation 測定が拾った。**

### 誤り 1: S1「壊れた machine.toml なら 23 ページすべてに検出力が出る」

実測では **4 ページが壊れた machine.toml で現に 500 する**。`_base_context` の防御
（`machine_name()` / `machine_type()` / `focus_z()`）の**外**で machine.toml を読む経路が
あるため。

| URL | 防御外で machine.toml を読む箇所 |
| --- | -------------------------------- |
| `/settings` | `ConfigStore.read_machine_settings`（tomlkit パース） |
| `/pasting/paste_solder` | 同上（auto しきい値の現在値） |
| `/pasting/loading` | `state.machine().paste_dispenser` |
| `/posctrl/copper_detection` | `state.machine().paste_dispenser.pad_align` |

実装者はこの 4 本を明示除外して 19 ページで parametrize した（除外理由をコメントと class
docstring に記載）。**この判断を承認**。4 ページの 500 は MR2 前からの既存挙動で
（`_base_context` の 2 つの防御は MR2 以前から try/except を持っていた）、MR2 の防御では
守れない別課題。→ 残課題に記録。

転用自体は成功しており、`machine_name()` の try/except を外すと **19 本すべてが落ちる**
（修正前は 1 本も落ちなかった）。

### 誤り 2: M3「fixture 追加で既存の等値 assert にも prefix 兄弟の検出力が付く」

`startswith` mutation に対しては成立しない。**列挙時の兄弟除外は `_is_allowed` ではなく
`_leads_to_allowed` が行う**ため、既存の等値 assert は `_leads_to_allowed` 側の退行しか
検出しない。私はどちらの関数がその経路を通るかを取り違えていた。

実装者は当初足した `test_prefix_sibling_is_absent_from_ancestor_listing` が mutation で
落ちないことを自分で確かめ、既存テストとの完全重複だったので削除し、代わりに既存テストへ
`-evil` ディレクトリ存在の前提 assert を足した。**この判断を承認**（重複テストを残すより
正しい）。新規 2 本（列挙 400 / 選択 400）が唯一の検出源として機能している。

### 検出力ゼロだった 2 件の処置

| 対象 | 処置 |
| ---- | ---- |
| `API_VERSION` | 唯一の参照テストが `from webui.models import API_VERSION` して自分と比較する**完全な同語反復**で、うっかりのバンプが無音で通っていた。1 箇所だけリテラル（`"api_version": 1`）でピン。互換判定に使う定数なのでリテラル重複は正当 |
| `JobSpecInfo.loading_stages` の既定値 | 単体ではなく **`JobDefinition` と名前を共有する optional 全 9 フィールドの既定値一致**を検証する形に一般化（`TestJobSpecInfoMirrorsJobDefinition`）。既定値の手複製は「`models.py` は pydantic のみ import」という裁定済みの構造の代償なので、ドリフト検出をその境界に置くのは筋が通る。対象名を固定する assert も併設されており parametrize が空回りしない |

「既定値を持たせず required にする」案は採らなかった（`JobDefinition` 側が既定値持ちなので
required 化すると `/api/jobs` 以外の構築側に無意味な必須引数が増える）。この判断も承認。

## 残課題（MR2 では直さない）

- **壊れた machine.toml で 4 ページが 500 する**（上記）。MR2 前からの既存挙動。`/settings` は
  まさに直しに行く場所なのに開けないので UX の穴だが、`read_machine_settings` がパースを
  前提とするため whitelist フォームでは直せない。まともに直すなら「設定ファイルが壊れています」
  ページが必要で、計画書に無い機能追加。**別 MR 候補**
- `StateResponse.mainsail_url` が未解決の生値のまま（`/api/machine-info` は解決済み）。
  MR4 で frontend がどちらを真実とするか決めるときに再検討する
- `_validate_pcb` が `pcb_browse_allowed` を見ないため、`webui_state.json` に許可外パスが
  残っていると復元される。API の口は閉じたが state 経由の面が残る
- `JobSpecInfo` の多くのフィールドが `JobDefinition` の既定値を手複製している（pydantic 限定の
  制約の帰結）。ドリフトはテストで検出できるようにした

## 委譲の記録

- MR0: 委譲せず自分で実装（フック 1 本 + テスト。数回のツール呼び出しで終わる規模）
- MR1: 実装 1 体（plan-implementer / high）→ レビュー 3 体（code-reviewer / xhigh、
  視点を計画準拠・並行性・テスト品質に分離）→ 反証 1 体 → 修正 1 体。
  独立した 3 視点のうち 2 名が同じ穴（prune のロック外書き込み）を指摘したのは
  多視点の効果。ただし反証で「import は全量上書き」と判明し却下に至ったのも、
  指摘をそのまま採らずに裏取りした効果。
