# docs-keeper: copper-alignment

## 整備内容

- `src/pcbasm/posctrl/README.md` に 1 行追記:
  「設計銅箔のカメラ投影と観測エッジ照合（chamfer）による位置ずれ算出」
  （README が posctrl の機能を箇条書きしているため、新規公開 API に合わせた最小更新）

## 整備不要と判断したもの

- ルート README — スクリプト一覧・モジュール説明なし（セットアップ手順のみ）
- `docs/` — design/path-motion-redesign.md のみで銅箔関連の言及なし
- `src/pcbasm/vision/README.md` 他 — copper/銅箔への言及なし（grep 確認）
- docstring — `posctrl/copper.py`・`scripts/posctrl/copper_detection.py` とも
  日本語・「.」終端・Args/Returns 規約に準拠済み。修正不要
  （`main()` の docstring 欠落はエントリポイントで自明のため付与せず）

## 検証

- `make format` グリーン（mdformat 含む全フック Passed）
