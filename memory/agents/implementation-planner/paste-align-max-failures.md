# 実装計画: paste_solder — 銅箔照合失敗数が許容数を超えたら塗布を即中止

ユーザー承認済み計画（2026-07-10）。planner 工程は本ファイルで完了扱い。

## 要件

- 塗布ジョブ（paste_solder）の部品ごと銅箔照合で、失敗部品数が許容数 n（machine.toml オプション設定、デフォルト 0）を**超えた時点で即中止**（残り部品の照合をスキップ、高さ計測・塗布に進まない）
- 中止は `raise ValueError(メッセージ)` → JobStatus.FAILED（`initial_purge_error` の前例 src/webui/jobs/pasting.py:763-764 と同じ流儀）。JobAborted は使わない
- 設定キーは `[paste_dispenser.pad_align] max_failures`（int, デフォルト 0）。**configs/*/machine.toml・data/testing/machine.toml には追記しない**（デフォルトで動く）。WebUI 設定ページには既存機構により空欄（placeholder「未設定」）で出る
- board_tour（src/webui/jobs/posctrl.py）は対象外・従来挙動のまま（max_failures を渡さない）
- デフォルト 0 は挙動変更（従来: 失敗部品は警告ログ + 無補正で塗布続行）— 合意済み

## 公開 IF（確定シグネチャ — spec-test-author / plan-implementer 並列作業の契約）

```python
# src/webui/jobs/board_ops.py に新規公開純関数
def pad_align_abort_message(
    failed_designators: Sequence[str], max_failures: int | None
) -> str | None: ...
# 契約: max_failures is None → None（無制限）
#       len(failed_designators) <= max_failures → None（境界: 失敗数 == 許容数は許容）
#       超過 → 失敗数・許容数・全 designator を含む日本語メッセージ文字列
# メッセージ基準形（テストは完全一致でなく部分一致で検証すること）:
#   f"銅箔照合の失敗部品数が許容数を超えました（失敗 {len(...)} / 許容 {max_failures}）: "
#   f"{', '.join(failed_designators)}。基板の向き・種類を確認してください"

# align_component_groups — kw-only 引数 max_failures を追加（デフォルト None = 従来挙動）
def align_component_groups(
    ctx: JobContext,
    session: PadAlignmentSession,
    groups: Sequence[ComponentPads],
    *,
    on_failure: Callable[[ComponentPads, int], None] | None = None,
    max_failures: int | None = None,
) -> list[tuple[ComponentPads, PadAlignmentResult]]: ...
# 失敗部品数が max_failures を超えた時点で、失敗 designator 一覧付き ValueError を raise

# src/pcbasm/config.py — PadAlign に属性追加
max_failures: int = 0  # 照合失敗の許容部品数。超過で塗布ジョブを即中止

# src/webui/config_store.py — MACHINE_FIELDS の pad_align ブロックに追加
FieldSpec("paste_dispenser.pad_align.max_failures", "照合失敗の許容部品数", "int")
```

## src/ 変更（plan-implementer 担当。tests/ は触らない）

1. **src/pcbasm/config.py** — `PadAlign`（:46-57）末尾に `max_failures: int = 0` を追加（コメント付き）。`__attrs_post_init__` を**新設**し、bool と負値を ValueError で拒否（`PasteDispenser.__attrs_post_init__` の initial_purge_ul ガード :129-133 と同型。メッセージ例: `f"max_failuresは0以上の整数である必要があります: {self.max_failures}"`)
2. **src/webui/config_store.py** — `MACHINE_FIELDS` pad_align ブロック（:89-102）の末尾（blur_ksize の直後）に上記 FieldSpec を追加（`_grouped_fields` の groupby のためブロック内連続配置必須）。`_coerce` の `case "int":`（:186-190）に max_failures キー限定の負値拒否を追加（float case の initial_purge_ul :172-176 と同型、`UnknownFieldError(f"{spec.key}: 0以上の値が必要です")`）。他の int キー（camera.device_id / probe.min_samples / blur_ksize 等）の挙動を変えないこと
3. **src/webui/jobs/board_ops.py** — `pad_align_abort_message` を新設。`align_component_groups` の失敗分岐（:69-73）で `failed: list[str]` に designator を蓄積し、処理順 **log → on_failure → append → 判定** で、メッセージが返れば即 `raise ValueError(message)`。docstring に Args（max_failures: 「超過した時点で ValueError を送出し即中止。None は無制限（board_tour が使用）」）と Raises: ValueError 節を追記
4. **src/webui/jobs/pasting.py** — :785 の呼び出しに `max_failures=session.machine.paste_dispenser.pad_align.max_failures` を渡す。:771-772 のコメントに「失敗が許容数（pad_align.max_failures）を超えたら即中止」の 1 行を追記

**変更しない**: posctrl.py / settings.html / settings.js / CSS / common.py（SECTION_LABELS "paste_dispenser.pad_align" は :146 に既存）/ configs/*/machine.toml / data/testing/machine.toml / pasting.py :807-812 の「未照合 pad は無補正塗布」フォールバック（許容内失敗では引き続き有効）

## tests/ 変更（spec-test-author 担当。src/ は触らない）

すべて `make test-no-hardware` で走ること（@mark_hardware 不要）。既存の class TestXxx / parametrize / 部分一致 assert の規約に従う。

- **tests/pcbasm/test_config.py** — ①デフォルト 0（TESTING_DATA_DIR/machine.toml は pad_align セクション無し）②明示値読み取り（tmp_path に `[paste_dispenser.pad_align]\nmax_failures = 2` を書いた toml → 2。`test_initial_purge_ul_reads_explicit_value` :158-172 と同型）③負値拒否 `PadAlign(max_failures=-1)` → `pytest.raises(ValueError, match="max_failures")`
- **tests/webui/test_config_store.py** — ①欠落キー → read 値 None ②write 2 → reread 2 ③write 0 → reread 0（境界）④write -1 → UnknownFieldError。`test_read_covers_every_whitelisted_key`（:55-58）は FieldSpec 追加で自動網羅（無修正）
- **tests/webui/routers/test_settings_api.py** — ①GET: 欠落=None かつ value_type=="int" ②PUT 2 → 200・反映 ③PUT -1 → 400
- **tests/webui/jobs/test_board_ops.py（新規ファイル）** — `from webui.jobs.board_ops import pad_align_abort_message`、`class TestPadAlignAbortMessage`:
  - 許容内 → None: parametrize `([], 0)`, `(["R1"], 1)`, `(["R1","R2"], 2)`
  - `max_failures=None`（無制限）→ None: `(["R1","R2","R3"], None)`
  - 超過 → 文字列: `(["R1"], 0)` — "R1"・失敗数・許容数を**部分一致**で含む
  - 複数 designator 全列挙: `(["R1","C3","U2"], 2)` → 3 つすべて含む
- **tests/e2e/test_webui_e2e.py** — `TestAirPumpToggleOverRealHttp`（:307-337）の雛形で新クラス: PUT `paste_dispenser.pad_align.max_failures = 2` → GET で 2 → 隔離 tmp の `configs/kurousagi/machine.toml` 実読みで `max_failures = 2` 反映を確認
- **追加しない**: paste_solder 実駆動の中止統合テスト（Klipper 接続が必要で test-no-hardware では到達不能。ValueError → FAILED は tests/webui/jobs/test_manager.py:87-101 が汎用保証）。メッセージ完全一致 assert 禁止（部分一致のみ）

## 検証

`make format && make type && make test-no-hardware`、その後 `make test-e2e`。`make test`（実機）は実行しない。
