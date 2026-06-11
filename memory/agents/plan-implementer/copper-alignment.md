# plan-implementer: copper-alignment 判断ログ

## 公開 IF

計画書 §1 のシグネチャから逸脱なし。IF 変更通知なし。

## 計画内だが解釈を確定した点

1. **fill_mask の 2 パスは polygon ごとに実施し `np.maximum` で合成**
   計画の「exterior→255 / interiors→0 の 2 パス」を全 polygon 一括でやると、
   穴の中に入れ子になった別の銅箔島（ゾーン切欠き内の孤立パッド等）が
   後段の interiors 塗りで消える。polygon 単位でテンポラリに描いて OR 合成した。
   合成テストで入れ子島が 255 になることを確認済み。

2. **`CopperEdgeMatcher(crop_size=None)` のテンプレートはフレーム − 2×window_px**
   None を「全画面」と解釈すると探索領域クランプ後に search == template となり
   matchTemplate が 1×1（常に offset (0,0)）に退化する。窓を確保できる最大の
   中心領域とした。docstring に明記。

3. **adjust は「直近保持した match」を使用、移動後のリセットなし**（計画どおり）。
   メインループが毎フレーム match を更新するため、通常運用では adjust 間に
   新しい観測が入る。コマンドがフレームより速く連打された場合は移動前の
   match を再利用する可能性があるが、計画の「直近 match で 1 回補正」を優先した。

4. **表示する board 座標は anchor の board 座標**（起動時は
   `board_transform.inverse().apply(anchor)`、move 後は指定値）。
   adjust ではステージのみ動き anchor 不変なので表示も不変（設計意図位置）。

5. **投影の image_size は初回 capture したフレームの実サイズ**を使用
   （config の width/height ではなく）。表示・検出と確実に整合させるため。

## 既知の数値的注意（バグではない）

- `mean_distance_px` は TM_CCORR の float32 演算誤差で完全一致時に
  ごく僅かに負（〜1e-13）になり得る。テストで `== 0.0` を厳密比較する場合は
  `pytest.approx(0.0, abs=1e-6)` を推奨（spec-test-author への申し送り）。

## 合成データでの動作確認（手元スモーク、テストは spec-test-author 担当）

- 恒等変換 + ppm=10: 投影公式どおり pixel = center − ppm·b（stage anchor (0,0) 時）
- 符号ピン: stage +1mm(x) → 投影 +10px(x)
- Rotation(180): 中心点対称
- 穴付き polygon: 穴 0 / 外 255、内外リングのエッジあり
- 視野外 polygon: 全ゼロ（bbox フィルタ経路）
- はみ出し polygon: フレーム縁に偽エッジなし、fill は全面
- matcher: (+7,−4)px シフト復元、完全一致 (0,0)、空マスク None、
  窓外シフトは |offset| ≤ window で mean_distance 大

## 検証状況

- `make format` グリーン
- `make type` グリーン
- `make test` は親エージェントが合流後に実施（指示どおり未実行）
