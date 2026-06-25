# 吐出量キャリブレーション刷新 — pcbasm 側簡素化

ブランチ `feature/20260624/dispense-calibration`。対象は今回の新規/変更 pcbasm コードのみ
（`dispense_calibration.py` 新規、`applicator.py` の `draw_line`/`_draw_polyline`/
`_resolve_paste_height`、`fill_sequence.py` の `rate_cap` 関連、`settings.py`）。webui は不可触。

## 簡素化した内部実装

1. **`applicator._resolve_paste_height` の line 分岐を `slot_area()` 呼び出しに置換**（唯一の変更）
   - 変更前: `slot_area = path_length * bead_width + math.pi * (bead_width / 2.0) ** 2`
     をローカル計算（新規 `dispense_calibration.slot_area` と**完全に同一の式**）。
   - 変更後: `return amount / slot_area(path_length, bead_width)`。
   - `from pcbasm.pasting.dispense_calibration import slot_area` を追加。
   - 理由: スロット（stadium）近似式が applicator と dispense_calibration に二重定義されていた
     （plan-implementer ノートでも「同一式」と明記）。webui 側はこの面積見積もりに公開 `slot_area`
     を既に使用しており、applicator も同一関数に寄せることで「式は 1 箇所」になり、将来ズレない。
   - `area` / `dot` 分岐は別式（dot は円のみの `nozzle_area`）なので不変。`math` import は dot 分岐の
     `nozzle_area` で引き続き使用するため残置（orphan 化せず）。

## 検討したが敢えて残した箇所（投機・外科的原則で保留）

- **`fill_sequence._rate_cap()`**: `_effective_rate()` 1 箇所からのみ呼ばれるが、「実効的な吐出
  レート上限（`rate_cap=None` は `max_dispense_rate`）」という名前付き概念。インライン化すると
  `_effective_rate` 内に三項分岐が混ざり可読性が落ちる。plan-implementer が意図して分けた構造で、
  振る舞い上の重複ではないため温存。
- **`fill_speed_actual` / `_effective_rate` / `_dispense_time` の `self.path.length()` 再計算**:
  元から存在する構造で、今回 `rate_cap` 追加で新たに生じた重複ではない。`@attrs.frozen` のため
  キャッシュ導入は投機的最適化。外科的原則で触らない。
- **`dispense_rate_schedule` / `fill_speed_schedule`**: ガード以外の本体は既に `_linspace` に
  共通化済み。2 関数は別個の公開 IF（re-export 済み・webui から個別呼び出し）であり、これ以上の
  統合は公開 IF を壊す。冗長なし。
- **`draw_line` の `resolved_*` と `_draw_polyline` の `None`→既定解決**: `_draw_polyline` は
  `_fill`（apply 経由・解決済み値を渡す）とキャリブ（draw_line）の両方から共用される。`draw_line`
  側の事前解決は必要な非対称で、消すと挙動が変わるため不変。
- **`dispense_calibration.py` の各クラス/メソッド**: 全 `attrs.frozen`・単一責務・early return 済み
  で十分に簡潔。簡素化の余地なし。

## 公開 IF 維持の確認

- 変更はメソッド内部実装 1 箇所のみ。クラス名・メソッド/関数シグネチャ・戻り値型・
  `pasting/__init__.py` の re-export はすべて不変。
- `draw_line(... max_fill_speed=None, rate_cap=None ...)` の引数・戻り値も不変。
- 循環 import 無し（`dispense_calibration` は `pcbasm.geometry` のみ依存・applicator 非依存）。
  `import pcbasm.pasting` / `from pcbasm.pasting import PasteApplicator, slot_area` 成功を確認。
- targeted テスト 151 passed（変更前後で同数・同内容）。`_resolve_paste_height` の line/auto 高さ
  検証（`test_line_height_uses_amount_over_slot_area`, `test_auto_height_uses_slot_area`）も通過。

## 検証結果

- `uv run pyright src/pcbasm/pasting/{dispense_calibration,applicator,fill_sequence}.py`
  → **0 errors / 0 warnings**
- `uv run pytest tests/pcbasm/pasting/{test_dispense_calibration,test_applicator,test_fill_sequence,test_settings}.py -m "not hardware" -q`
  → **151 passed**（簡素化前後で不変）
- `make format` / `make test`（フル）・ハードウェアテスト: 指示により未実行（親が合流時に 1 回実行）。
