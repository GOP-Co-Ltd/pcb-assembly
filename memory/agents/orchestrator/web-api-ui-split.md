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

## 委譲の記録

- MR0: 委譲せず自分で実装（フック 1 本 + テスト。数回のツール呼び出しで終わる規模）
- MR1: 実装 1 体（plan-implementer / high）→ レビュー 3 体（code-reviewer / xhigh、
  視点を計画準拠・並行性・テスト品質に分離）→ 反証 1 体 → 修正 1 体。
  独立した 3 視点のうち 2 名が同じ穴（prune のロック外書き込み）を指摘したのは
  多視点の効果。ただし反証で「import は全量上書き」と判明し却下に至ったのも、
  指摘をそのまま採らずに裏取りした効果。
