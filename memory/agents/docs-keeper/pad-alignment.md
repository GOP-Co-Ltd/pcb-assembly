# pad-alignment ドキュメント整備ノート

## やったこと

1. **`src/pcbasm/posctrl/README.md` に最小追記（2 行）**
   - bullet 追加: 「pad 単位の銅箔照合による自動位置合わせ（PadAligner）」
   - 末尾に Observer 契約の 1 文: `observe() -> Transform`（カメラ mm 空間、
     原点=画像中心、想定→観測）で統一している旨。
   - 既存の記述粒度（箇条書き＋短文）に合わせた。書き直しなし。

## 確認のみ（修正不要だった箇所）

- `position.py` — XYPositionAdjustor のクラス/Args docstring は新契約
  （observe: Transform 返却、offset_transform 必須）と一致。
- `offset.py` — OffsetTransformMeasurer の observe Args 記述は新契約と一致。
- `setup.py` — OffsetObserver のクラス/observe docstring は
  `observe() -> Transform` 契約を明記済み。
- `board_tour.py` — モジュール docstring の処理順 9 が
  「全パッドを巡回し、銅箔照合で自動位置合わせ」に更新済み。
- 新規 `pad.py` / `correction.py` — docstring は実装と整合
  （符号アンカー (A2)/(A4) の記述含む）。

## 検証

- `make format` パス。
