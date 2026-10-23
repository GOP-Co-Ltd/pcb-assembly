# 外周余白（boundary_margin）を負にできるようにする

ブランチ: `feature/2026-10-23/negative-outline-margin`（main から worktree で分岐）

## 1. 計画

要求: はんだ塗布の外周余白にマイナスを設定し、pad を少しはみ出して塗れるようにする。

ユーザー確認済み（2026-10-23）: 下限は設けない。どの値でも「ビード外縁が pad 外形から
`-boundary_margin` mm はみ出す」という意味で揃える。

- `inset = boundary_margin + w / 2` の式はそのまま。inset が負になったら pad を外側へ膨らませる
- 公開 IF の変更
    - `PasteDispenser.validate_values`: `boundary_margin` を非負の制約から外し、有限な数値であることだけを求める
      （machine.toml の既定値と pad 単位の override の両方で負を受け付ける）
    - `pcbasm.geometry.offset_components(polygon, depth)`: `depth < 0` なら `buffer(-depth)` で外側へ広げる
      （これまでは `depth <= 0` で入力をそのまま返していた。呼び出し元は fill_path だけ）
- 線塗布: `_generate_linear_path` は end_inset が負なら両端を外へ延ばす（今のコードのままでそうなる。docstring だけ直す）
- テスト観点
    - 負の値が検証を通る（params / config）。NaN は従来どおり弾く
    - 面塗布: 外周トレースの頂点の符号付き距離（内側を正）の最小値が inset に等しい（正・負の inset の両方）
    - 線塗布: inset が負のとき両端が pad の外へ |inset| ずつ延びる
    - `offset_components` に負の depth を渡すと面積が増えた 1 成分を返す
- UI: pad 表と設定画面の数値欄には min が付いていないので変更不要
- リスク: 負にすると凸の角が丸くなる（shapely の buffer の既定 join）。はみ出しは少量の想定なので許容する

## 2. テスト実装

- 受理: `test_params` / `test_config` に負の値の受理テストを足し、拒否ケースの `-0.01` は NaN に置き換えた
- 経路: `TestNegativeExteriorMargin`（外周トレースの符号付き距離 = inset、線の両端延長、0.8 角 pad が負の余白で area になる）
- `offset_components` の負の depth。red は 4 件。線の延長と方式判定は今のコードでも通るが、仕様を固定するために残した

## 3. 機能実装

- `validate_values`: `boundary_margin` を非負の集合から外し、有限値の検査だけを残した
- `offset_components`: `depth != 0` で buffer する。線塗布は元から負の end_inset で両端が延びるので、docstring だけ直した

## 4. 自己レビュー（code-reviewer に委譲、verdict approve）

- should-fix: 塗布量は元の pad 面積 × `ul_per_mm2` のままで、広げた経路へ薄く延びる → 対応しない。
  `ul_per_mm2` は pad 面積あたりの量という定義どおり。量を増やすかはユーザーに伝えて判断を仰ぐ
- should-fix: HTTP 境界で負の値を受け付けるテストがない → `test_settings_api` の PUT 成功ケースに負の値を足した
  （pad override も同じ `validate_param_values` を通る）
- nit: params の docstring → 段階 5 で追記。inf のテスト追加 → 見送り（NaN と同じ有限値チェック）
- nit: auto の方式判定は余白の影響を受けない（小さい pad は dot のまま）→ 既存の仕様どおり。ユーザーに伝える

## 5. ドキュメント

- `PasteParams` の docstring と `config.py` のコメントに負の意味を追記した。README に余白の記述はない
