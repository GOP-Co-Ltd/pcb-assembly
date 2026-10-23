# 塗布方式 Auto の実際の方式を pad 表に出す

ブランチ `feature/2026-10-23/show-resolved-dispense-mode`。solo-dev-cycle で進める。

## 1. 計画

### 要求

pad 編集表の「塗布方式」列で、選択値が Auto（継承の結果が Auto を含む）のとき、実際に塗る方式（点 / 線 / 面）を隣に出す。

### 計測（Raspberry Pi 5、`data/testing/config/machine.toml`、全 pad を auto で解決）

| 基板                     | pad 数 | GET 構築 | 全 pad の FillPlan.for_pad |
| ------------------------ | -----: | -------: | -------------------------: |
| fill_coverage（大 pad）  |      8 |    6 ms  |                      35 ms |
| led_blinker              |     18 |   10 ms  |                       2 ms |
| uploads/LSPB             |     67 |   87 ms  |                      17 ms |
| uploads/GENS_Power_5     |     93 |  212 ms  |                      24 ms |

経路生成のほぼ全部が面塗布の外周トレースとジグザグ（`_outline_and_zigzag`）。`offset_components` は 1 ms 未満。
pad の面積に比例して重くなるので、大きい pad が多い基板では GET の数倍になる。

### 方針

- GET で経路は作らず、方式だけを判定する。`FillPlan.build` のフォールバック判定を `_applied_mode` に切り出し、
    `build` と新設の `FillPlan.mode_for_pad` が同じ判定を使う（表示と実際の塗布が構造的に一致する）
- 面塗布の成立判定は `offset_components(polygon, inset)` が空でないこと。`_area_fill` が空になるのは成分が無いときだけなので同値
- 集計と表示文字列は pcbasm の新モジュール `pcbasm/pasting/applied_mode.py`。ラベルは `PASTE_PARAM_FIELDS` の
    dispense_mode の choices から引く（micro_line が増えても表示側は無修正）
- backend は `HierNodeInfo.applied: dict[str, AppliedValueInfo]`（field 名 → 表示）。JS は field 名を直書きせず、
    choice セルで `node.applied[field]` があれば span を出すだけ

### 表示の規則

- 出すノード: 解決済みの方式が auto、または配下の有効 pad の実際の方式が解決済みの方式と 1 つでも違う
    （フォールバック・子孫の override）。選択値から実際の方式が読み取れるノードには出さない
- 数えるのは有効 pad だけ（無効 pad は塗らない）。有効 pad が無いノードには出さない
- 1 方式だけ: `→ 面`。混在: `→ 点 12 / 線 6`（選択肢の並び順、0 件は出さない）
- title（ツールチップ）: `実際の塗布方式（有効 pad 18 件）: 点 12 件、線 6 件`

### 公開 IF

- `FillPlan.mode_for_pad(polygon, *, config, params) -> AppliedDispenseMode`
- `pcbasm.pasting.applied_mode.AppliedModeSummary(counts: dict[str, int], label: str, title: str)`
- `pcbasm.pasting.applied_mode.applied_mode_summaries(hierarchy, pad_settings, node_settings, config) -> dict[HierKey, AppliedModeSummary]`
- `web.api.routers.pasting_view.AppliedValueInfo`、`HierNodeInfo.applied`

### テスト観点

- `mode_for_pad` が全方式 × 面 / 線 / 点 / フォールバック / 不正ポリゴンで `for_pad(...).dispense_mode` と一致
- 集計: auto で混在 → 件数とラベル、単一 pad → `→ 面`、明示の方式がそのまま通るノードは出ない、
    面のフォールバックは出る、無効 pad は数えない
- API: GET tree に applied が載り、件数が fill-path の per-pad 方式と一致。L0 を dot にすると消える
- E2E: L0 行に server のラベルが出て、dot を選ぶと消える

### リスク

- PR #48（micro_line）が `FillPlan.build` の match に case を足すので fill_path.py で衝突する。
    解消は `_applied_mode` に micro_line の判定（微小線の inset で `_line_fill` が成立するか）を足し、build の match に経路生成を足す

## 2. テスト実装

- `tests/pcbasm/pasting/test_fill_path.py`: `TestFillPlanForPad` に `mode_for_pad` と `for_pad` の突き合わせ
    （`DISPENSE_MODES` 全部 × 8 形状）。micro_line が `DISPENSE_MODES` に入れば自動で対象になる
- `tests/pcbasm/pasting/test_applied_mode.py`（新規）: auto の混在・単一、明示方式の省略、線 → 点・面 → 線のフォールバック、
    明示の親の下の子孫 override、無効 pad
- `tests/web/api/routers/test_pasting.py`: GET tree の applied が fill-path の per-pad 方式の件数と一致、L0 を dot にすると全ノード空
- `tests/e2e/test_paste_solder_browser.py`: L0 行にサーバーのラベルと title が出て、dot を選ぶと消える

実装前に 34 件が期待どおり失敗（AttributeError / KeyError / ImportError）。

## 3. 機能実装

- 計画どおり。`FillPlan.mode_for_pad` は `for_pad` と違い Self を返さないので `@staticmethod` にした
- pyright の reportPrivateUsage を避けるため、突き合わせテストは別クラスにせず `TestFillPlanForPad` に入れた（`_paste` を使うため）

## 4. リファクタリング・自己レビュー

- `_area_fill` が空 ⇔ `offset_components` が空、を確認。成分は空でない Polygon だけで、`_outline_and_zigzag` は外周を必ず含む
- `applied_mode_summaries` の絞り込みで解決済み方式を 2 回引いていたのを変数に出した
- 実装後の計測: 集計は fill_coverage 2.5 ms、led_blinker 2.8 ms、LSPB 9.9 ms、GENS 12.2 ms（GET 全体は 7〜258 ms）
- 却下した案
    - GET で `FillPlan.for_pad` を全 pad に回す: 実装は最小だが、面塗布のジグザグが pad 面積に比例して重く、大 pad の基板で GET の数倍になる
    - 「塗布パス計算」ボタンの応答に載せる: 編集のたびに表示が消え、Auto を選んでもボタンを押すまで何も出ない。表は両レイヤーのノードを並べるが fill-path はレイヤー単位
    - 判定を `build` と別に書く: 表示と実際の塗布がずれ得る。build と同じ `_applied_mode` を通す形にした

## 5. ドキュメント

- `fill_path.py` の module docstring に `mode_for_pad` と判定の共有を追記
- `src/pcbasm/pasting/README.md` のモジュール表に `fill_path.py` の `mode_for_pad` と `applied_mode.py` を追記
