# MR4: はんだ塗布ページの任意位置パージ

計画書 `docs-image-based-dispense-calibration-m-generic-frog.md` の MR4 節に対応。
ブランチ `feature/2026-09-08/purge-point`（MR2 ブランチから分岐、target も MR2）。

## 要件

初回パージの対象を「基板上の pad」から「基板上の任意位置」へ拡張する。はんだ塗布
ページで「パージ位置を設定」ボタンを押し、基板ビューの任意位置をクリックすると
そこがパージ位置になる（マーカー表示）。

## 公開インターフェース

### `pcbasm.pasting.initial_purge`

```python
@attrs.frozen
class ResolvedInitialPurge:
    amount_ul: float
    point: Point2d            # board 座標の塗布点（pad 由来なら pad 中心）
    label: str                # 表示用（pad id か座標文字列）
    pad: Pad | None           # pad 由来なら pad。位置合わせに使う
    pad_id: str | None
    source: Literal["point", "pad", "default"]

def resolve_initial_purge(
    *, amount_ul: float, point: Point2d | None, pad_id: str | None,
    hierarchy: PadHierarchy, routed_pads: Sequence[Pad], outline: Polygon,
    layer: Layer = Layer.TOP,
) -> tuple[ResolvedInitialPurge | None, str | None]
```

優先順位は **point > pad_id > 順路先頭**。point が基板外形の外ならエラー文を返す。
`source` の `"explicit"` は `"pad"` へ改名（point と並べて意味が通るように）。

### `pcbasm.pasting.settings` / `persist`

- `PasteSettingsModel.initial_purge_point: Point2d | None`
- `with_initial_purge_point(point)` / `with_initial_purge_pad_id(pad_id)` は互いを
    `None` にする（相互排他を生成メソッドで担保）。`with_initial_purge_pad_id(None)` は
    「自動に戻す」＝両方クリア
- 保存 JSON は `settings.initial_purge_point: [x, y]`。キー追加のみなので
    `BOARD_SETTINGS_SCHEMA_VERSION` は 1 のまま（欠落は `None`）

### `pcbasm.pasting.session`

- `point_transform(point, correction)` を追加（`pad_transform` の点版。補正は
    `correction_for(point)` で内挿する）

### API

- `InitialPurgePatch.point: list[float] | None`。`point` と `pad_id` の同時指定は 400
- `InitialPurgeInfo.point` / `ResolvedInitialPurgeInfo.label` を追加、
    `ResolvedInitialPurgeInfo.pad_id` を `str | None` へ
- `selection_label` は従来どおりサーバーが組む（座標指定時は `座標 (x, y)`）

## 実装ステップ

1. core: `initial_purge` の点対応 → `settings` / `persist` → `session.point_transform`
2. `workflow.PasteTargets.alignment_pads` を `pad is None` に耐えさせる
3. `paste_solder` ジョブ: pad 由来なら `pad_transform`、点由来なら `point_transform`
4. API: patch / info / build_initial_purge
5. `BoardSettingsStore.prune` が外形外の点も落とす（自分の変更で生まれた
    「保存済みの点が外形外 → GET が 400 でページが開けない」経路を塞ぐ）
6. UI: 「パージ位置を設定」ボタン + マーカー打ちモード + `renderPurgeMarker`

## テスト観点

- point 解決の優先順位（point > pad_id > 順路先頭）、外形外エラー、pad と point の相互排他
- persist の round-trip と欠落時 `None`、不正な point の拒否
- PATCH の point 保存・同時指定 400・クリア
- prune が外形外の点を落とす
- ページに「パージ位置を設定」があること、e2e でマーカー打ち → 再読込で永続

## 実装時の判断（計画外）

- **`BoardSettingsStore.prune` へ `outline` を足した。** 保存済みの点が外形外へ出ると
    `build_initial_purge` が 400 を投げてページ自体が開けなくなる。自分の変更で生まれた
    経路なので prune で落とす（pad id の孤児処理と同じ扱い）
- **`.pad-initial-purge-actions` を 2 列から 3 列へ変えた。** ボタンを 1 個足すと行が増え、
    基板ビューが 1280x720 のビューポート外へ 0.4px はみ出して
    `test_pad_svg_contains_visible_polygons` が落ちた（overflow は幅ではなく高さ）。
    3 ボタンを 1 行に収めて行数を元に戻した
- **e2e のクリック対象は `pad-outline` ではなく SVG 本体。** 外形線は `fill: none` なので
    Playwright のヒットテストを通らない
- **`ResolvedInitialPurge.pad` を `Pad | None` にした。** 位置合わせ pad の追加
    （`alignment_pads`）と精密照合付き transform は pad があるときだけ使う
- `renderPurgeMarker` は `appendEndpoint` と円＋文字の作りが似ているが、共通化は
    しない（MR4 の範囲外の既存コードへ波及する）

## 座標一本化（ユーザー指示による作り直し）

「初期パージ位置（自動）にパッドを選択するようになっていますが、開始パッドの中心の座標を
扱い、そもそもパージはパッドではなく、座標で扱うようにしてください」という指示で、pad
ベースの経路を全廃した。

- `PasteSettingsModel.initial_purge_pad_id` を削除。保存するのは `initial_purge_point` だけ
- `ResolvedInitialPurge` は `amount_ul` / `point` / `label` / `source`（`explicit` |
    `default`）のみ。`pad` を持たないので `PasteTargets.alignment_pads` と `_same_pad` も削除し、
    位置合わせは順路 pad だけになった
- 塗布 transform は常に `point_transform`（pad の designator 付き精密照合は使わない）
- API は `point` のみ。pad 指定・相互排他・pad 存在検証・レイヤ検証が全部消えた
- UI から「選択パッドを設定」を撤去。パッドはビュー上に見えているので、そこをクリックすれば
    同じことができる。ボタンが 2 個に戻ったので `.pad-initial-purge-actions` も 2 列へ戻した
- 既存の保存済み `initial_purge_pad_id` は decode で黙って捨てられ、自動（順路先頭の中心）へ
    戻る。schema version は据え置き（バージョンを上げると旧ファイルを読めなくなるだけで、
    pad → 座標の移行は persist 層から PCB を読めないので実行できない）

## 銅箔表示

パージ位置を選ぶための背景として銅箔島を基板ビューへ描く。

- `GET /api/pasting/pad-config/copper` を新設。pad-config は編集ごとに取り直すので、
    点数の多い銅箔はそこへ載せず別エンドポイントにして基板ごとに 1 回だけ取る
- `pcbasm.geometry.display_rings(polygon, tolerance, precision)` で簡略化。既定 0.02mm で
    led_blinker が 64 KiB → 7.4 KiB（実測）。穴は環として保ち、SVG は
    `fill-rule="evenodd"` の `<path>` で描く
- `.pad-copper` と `.pad-viewer-picking .pad` に `pointer-events: none`。マーカー打ち中に
    パッドが hover やカーソルで反応すると座標を狙えない
