# MR2: レイアウト preview API + SVG パネル（solo-dev-cycle）

ブランチ `refactor/2026-09-08/paste-dataset-dot-job`（MR1 `refactor/2026-09-08/paste-dataset-dot-core`
の上に stacked。MR の target も MR1 ブランチ）。

## 段階 1: 計画

### 公開インターフェース

`src/pcbasm/pasting/dataset/plan.py` に追加:

```python
@attrs.frozen
class DotGridPreview:
    """設定が配置不能でも描ける診断用レイアウトと派生カウント."""
    spec: DotGridSpec
    plate: Rect
    usable_area: Rect | None      # spec 不正時は None
    purge_cell: Rect | None
    grid: tuple[Rect, ...]        # 格子セル全部（パージ除外前・未使用も含む）
    cells: tuple[DotCell, ...]    # 配置できたとき中身、できなければ空
    blanks: tuple[DotBlank, ...]
    capacity: int
    sample_count: int
    target_count: int
    volumes_ul: tuple[float, ...]
    view_count: int               # 中心込みの総 view 数
    image_count: int              # target_count * view_count * 2
    error: str | None

def preview_dot_grid(
    spec: DotGridSpec, *, view_count: int, view_offset_mm: float
) -> DotGridPreview
```

派生カウント（撮影枚数など）をコアへ入れるのは、router / JS で再導出させないため
（skill `webui-thin-wrapper`）。これで router のレスポンスは `mirror_model(DotGridPreview)`
1 行になり、同期すべき箇所が 1 つになる。

`src/web/api/jobs/pasting/dataset.py` の public 化:

```python
def grid_spec_from_params(params: Mapping[str, ParamValue]) -> DotGridSpec
def resolve_shuffle_seed(value: int) -> int   # 0 なら乱数
```

seed の乱数化を `grid_spec_from_params` から分離する。preview が毎キーストロークで
別配置になるのを避け、seed 0 のときは決定論的な配置を描いて「実行時に別シードで
再配置される」ことを UI 文言で伝える。

`src/web/api/routers/paste_dataset.py`（新規）:

```
POST /api/pasting/paste-dataset-layout
  request : ジョブ ParamSpec と同名のフラットな値（strict / extra=forbid）
  response: mirror_model(DotGridPreview)
```

配置不能でも 200 で返し、理由は body の `error` に載せる（テスト塗布基板 preview と同方針）。

### 実装ステップ

1. コア: `DotGridPreview` / `preview_dot_grid`
2. ジョブ: `grid_spec_from_params` / `resolve_shuffle_seed` の切り出し
3. router: 新規モジュールと `app.py` への登録
4. テンプレート: summary 行 + エラー表示 + SVG のパネル
5. JS: `paste_dataset_layout.js`（debounce POST → SVG 描画、数値は再導出しない）

### テスト観点

- `preview_dot_grid`: 有効設定 / 容量不足 / spec 不正（負寸法）/ view 不正の 4 系統。
    `grid` が未使用セルを含むこと、`image_count` の値、`view_count` が中心込みであること
- router: 200 と主要フィールド、容量不足で 200 + body error、未知キーで 422、
    文字列を数値欄へ渡して 422（strict）
- SSR: パネルの DOM フック
- e2e: 実サーバ経由で SVG にセルが描かれること

### リスク

- MR1 が未 merge なので stacked。MR の target は MR1 ブランチ。GitLab の pipeline は
    `merge_request_event` が必要（記憶 `project-pasting-restructure-mrs`）
- `mirror_model` は `tuple[X, ...]` を `list[X']` にするので、`Rect | None` の入れ子が
    mirror できるかを最初に確かめる（できなければ `usable_area` を必須にして
    plate と同値にフォールバックする）

## 段階 2〜3: テスト先行と実装

計画どおり。red の確認（ImportError / 404 / DOM 欠落）→ 実装 → green。

計画外の判断:

- **`preview_dot_grid` のために `plan_dot_grid` から `_grid_geometry` を切り出した**。
    配置不能でも有効領域・パージ・格子を返すため、幾何計算と配置可能性判定を分ける必要が
    あった。`plan_dot_grid` の公開 IF と挙動は不変
- **`mirror_model` の戻り値を返り値型に書くと pyright が `reportInvalidTypeForm`**。
    既存 `paste_flow_calibration_board.py` と同じく `# type: ignore[valid-type]` を付けた
- **e2e で操作権を取らずに入力を変更しても発火しない**。`control.js` が
    `data-requires-control` の要素へ `inert` を付けるため。閲覧者でも図が読めることは
    操作権なしのテストで、設定変更の追従は操作権ありのテストで見るよう 2 本に分けた

## 段階 4: 自己レビュー（diff 根拠）

指摘と対応:

1. **`view_count` の意味衝突** — リクエストは周辺 view 数（4）、レスポンスは中心込みの
    総数（5）で同名だった。レスポンス側を `views_per_cell` へ改名した
2. **レイアウト項目名が 3 箇所（ジョブ ParamSpec / リクエストモデル / JS）に重複** —
    ジョブへ項目を足して API / JS を忘れると preview が黙って無視する。
    `TestLayoutFieldNamesStayInSync` で「リクエストモデル == レイアウト ParamSpec」と
    「JS が全項目名を含む」をピンした
3. **JS が古いレスポンスで描き得た** — debounce 中に応答順が入れ替わると stale 表示に
    なるので世代番号でガードした

却下:

- リクエストモデルを `ParamSpec` から動的生成する案。ドリフトは契約テストで十分検出でき、
    生成にすると型が読めなくなる（「要求されていない抽象化を追加しない」）
- 配置不能時に図を完全に消さない要件を、板寸法自体が不正な場合まで広げる案。描くべき
    幾何が無く、理由文で足りる

## 段階 5: ドキュメント

`docs/image-based-dispense-calibration.md` の WebUI 節へ「配置プレビュー」を追加
（表示内容・操作権不要・配置不能時の方針・送らないパラメータ）。
