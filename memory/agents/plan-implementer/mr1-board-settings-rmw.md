# MR1 — 同時編集で設定が黙って失われる RMW を排他化

計画書: `docs/plans/web-api-ui-split.md`（origin/docs/20260730/web-api-ui-split）の「### MR1」節のみ。

## 計画外の判断ログ

- **`JobManager.start` の既定値保存も merge へ**（`jobs/manager.py:449` 相当）。計画は
  `routers/jobs.py:115-119` と `jobs/manager.py:556-563` の手書き二重マージ削除だけを指示していたが、
  `save_job_param_defaults` を `merge_job_param_defaults` に**置き換える**指示のため、この 3 番目の
  呼び出しも merge へ移した。セマンティクスは「置換 → マージ」に変わる。`persisted_params` は
  `validate_params` 後の値なので定義済みキーは常に揃っており、実挙動の差は無い（既存テスト全通過）。
- **`AppState._persist` は `_persist_lock` を取らない**。`threading.Lock` は非再入なので、
  `merge_job_param_defaults`（ロック保持中に `_persist` を呼ぶ）とデッドロックする。ロックは
  呼び出し側（`select_pcb` / `merge_job_param_defaults`）が保持し、`_persist` の docstring に明記した。
  `select_pcb` は `machine_lock` の内側で `_persist_lock` を取るので、計画の順序固定と一致する。
- **テストの JobManager DI 用に `tests/webui/jobs/conftest.py` へ `make_board_store()` ヘルパと
  `board_store` fixture を追加**。`real_manager` / `checkerboard_manager` は settings が別なので
  fixture ではなくヘルパ関数を直接使う。
- **`expected_pcb` の 409 detail 文言**は「PCB が切り替わりました。ページを再読み込みしてください」
  とした（計画は日本語であることのみ指定）。
- **`expected_pcb` の router テストを追加**（`tests/webui/routers/test_pasting.py::TestExpectedPcb`）。
  計画のテスト一覧に無いが、新規の 409 分岐が無検証になるため。
- JS 側は `withExpectedPcb(body)` 1 関数で 3 経路にまとめた。値は `GET /api/pasting/pad-config` の
  `pcb_file` をそのまま転送するだけで、判定はサーバに閉じている（skill `webui-thin-wrapper`）。

## 他 implementer への IF 変更通知

- `JobManager(state, preview, catalog, settings, board_store, *, log_capacity=500)`
  — `board_store` は**位置引数で必須**。`app.state.board_store` と同一インスタンスを渡すこと。
- `AppState.save_job_param_defaults` は削除。`merge_job_param_defaults(job_name, values) -> dict` を使う
  （呼び出し側でのマージは不要。戻り値がマージ結果）。
- `BoardSettingsStore.update(source_pcb, base_config, *, board_signature=None, mutate)` を追加。
  PATCH 経路の RMW は `save` ではなく `update` を使う。`save` は単発上書き専用
  （export/import/prune は現状維持）。
- pad PATCH 3 本の request model に `expected_pcb: str | None = None` が入った。

## 既知の制約・残課題

- 排他は**プロセス内ロック**のみ。複数 backend プロセスが同じ `data_dir` を共有する構成は想定外
  （書き込み自体は atomic replace なので torn read は起きないが lost update は起きる）。
- `TestUpdate::test_concurrent_updates_of_distinct_nodes_both_survive` は実スレッドのレース検出のため
  確率的（ロックを外すと 10 回中 7 回 fail）。ロック内再ロードの**決定的**な証明は
  `test_reloads_inside_lock_so_external_write_is_not_lost` が担う。ロックがある状態では 10/10 green。
- `patch_initial_purge` は machine.toml 書き込み（`machine_lock`）と board JSON 書き込み
  （`update` のロック）が別トランザクション。両者をまたぐ原子性は MR1 の範囲外。
- 実機確認（2 ブラウザで別 pad を同時編集 / ジョブ実行中の pad 編集）は未実施。ユーザー担当。

## 既知の制約（MR1 では直さない）

レビュー裁定（`memory/agents/orchestrator/web-api-ui-split.md`）で「別 MR 候補」「残課題」
として記録された事項。

- **`prune` / import は `_update_lock` の外で書く。** どちらも「アップロード済み doc / 呼び出し側
  が持つモデルからの全量上書き」で保存済み JSON を読まないため RMW ではないが、pad PATCH と同時に
  走ると `update` のロック内 load → save の sub-ms の窓で pad 編集を取りこぼしうる。ロック化には
  `save` の再入回避リファクタが伴うため別 MR 候補。
- **`board_signature` はロック外で読んだ値を書き戻す。** `.kicad_pcb` の差し替えとリクエストの
  重なりが同時に起きると signature が巻き戻りうる（S1 → S2 → S1）。直すには signature 再計算
  （PcbFile パース + 階層構築）をロック内に持ち込むことになり、計画書の「PCB のパース・階層構築・
  入力検証はロック外」指示に反する。`save` 版でも同じ挙動で MR1 由来ではない。
- **`write_text_atomic` は fsync しない。** 同一ディレクトリの一時ファイル → `Path.replace` なので
  torn read（部分・空の内容の読み取り）は防ぐが、電源断・カーネルクラッシュ耐性は対象外。
  変更前の `config_store` / `board_settings` の書き込みと同水準。
- **`json.dumps(..., indent=2)` は Python 3.13 では C エンコーダを使う**（3.13 で indent が
  C 実装に対応した）。したがって `_persist` の dict 走査中に別スレッドが挿入しても
  `RuntimeError: dictionary changed size during iteration` は起きない。`_persist_lock` が実際に
  防いでいるのは「A が dumps → B が dumps して replace → A が古い snapshot で replace」による
  **ファイル側の巻き戻り**で、これを `test_concurrent_merges_never_roll_back_the_state_file` が
  ピンしている。

## レビュー指摘の修正（2 巡目）

`memory/agents/orchestrator/web-api-ui-split.md` の裁定で採用された 8 件に対応した。
テストの補強が中心だが、src に 1 点の実装バグ修正が入った。

- **`AppState.merge_job_param_defaults` が `_persist_lock` を取っていなかった**（docstring と
  計画書は「ロック内で読む → マージ → 永続化」を要求しているのに `with self._persist_lock:` が
  抜けていた）。`job_param_defaults` の読み取りロックも書き手がロックを取る前提でしか意味を
  持たないため、実装をドキュメントと計画書に合わせた。追加した巻き戻り検出テストは
  ロック無しで 5/5 fail、ロック有りで 10/10 pass。

## 検証結果

- make format: pass
- make type: pass（pyright 0 errors）
- make test-no-hardware: pass（1627 passed, 87 deselected）
  - レビュー修正後に再実行して pass（下記「修正後の検証」）
