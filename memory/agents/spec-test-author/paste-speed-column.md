# paste-speed-column（pad-table ヘッダ/ボディ列ずれのリグレッション固定）

per-pad の `fill_speed`（塗布速度）override 廃止に伴い、バックエンド `ResolvedSettings`
と JS の `FIELDS` からは削除済みだが、テーブルヘッダ
`src/webui/templates/partials/pad_editor.html` に `<th>塗布速度</th>` が取り残され、
ヘッダ列数がボディ列数より 1 多い（カラムが 1 つずれて見える）不具合を固定する。

## 書いたテスト一覧

- `tests/e2e/test_webui_e2e.py::TestPadTableHeaderOverRealHttp::test_pad_table_header_column_count_matches_resolved_settings`
  — 構造整合（エッジ寄りの回帰テスト）。pad-table の thead `<th>` 数が
  バックエンドの解決済み設定モデルと一致することを検証。

補助（module-level、テスト専用ヘルパ）:
- `_PadTableHeaderCounter(HTMLParser)` — `id="pad-table"` の thead 内 `<th>` を数える stdlib パーサ
- `_count_pad_table_header_columns(html)` — 上記を呼ぶ薄いラッパ

## 仕様根拠の対応表

- test_pad_table_header_column_count_matches_resolved_settings → 依頼仕様
  「テーブル構造はノード列 + 有効(enabled)列 + 各設定フィールド列。ヘッダの `<th>` 数は
  ボディの列数と一致すべき」。期待 `<th>` 数を
  `1(ノード列) + len(ResolvedSettings.model_fields)` で導出する
  （`ResolvedSettings` は `enabled` を含む 8 フィールド → 期待 9）。

## 参照した既存パターン / 判断根拠

- **live_server + ページ GET パターン**: 既存の
  `TestGenerateRectPcbOverRealHttp.test_page_and_job_artifact_are_served`
  （`httpx.get(f"{live_server.base_url}/pasting/generate_rect_pcb")` → `status_code == 200`
  → `page.text` を検査）に倣った。ブラウザ不要。
- **ページ URL = `/pasting/paste_solder`**: `src/webui/routers/pages.py` の
  `@router.get("/{tab}/{feature}")`（`feature_page`）と
  `FEATURE_TEMPLATES[("pasting","paste_solder")] = "pasting/paste_solder.html"`、
  および `paste_solder.html` が `{% include "partials/pad_editor.html" %}` する事実から特定。
  pad-table の thead はサーバーレンダリングの静的 HTML（tbody `#pad-table-body` だけ JS が埋める）
  なので、PCB 未選択でも GET だけで `<th>` を数えられる。
- **パーサ選択 = stdlib `html.parser`**: `pyproject.toml`/`uv.lock` に
  BeautifulSoup / bs4 / lxml が無い（dependencies は attrs, cattrs, fastapi, httpx, jinja2,
  matplotlib, numpy, opencv-python, python-multipart, scipy, shapely, tomlkit, uvicorn）。
  依存追加は避け、stdlib の `HTMLParser` で thead 内 `<th>` を数える。
- **期待値をモデルから導出**: `from webui.routers.pasting import ResolvedSettings` し
  `1 + len(ResolvedSettings.model_fields)` を使用。ハードコード（9 等）を避けることで、
  将来フィールドが増減したときにヘッダ更新漏れ（本バグと同種のずれ）を検出できる。
  testing-strategy 準拠でヘッダ文字列の完全一致ピンはしない（列「数」の構造整合のみ）。
- **3rd-party 表面・内部関数のモックなし**: 実 uvicorn（live_server）・実 HTML・実モデルのみ。

## 期待される失敗（仕様 first）

- **修正前**: `src/webui/templates/partials/pad_editor.html` の thead に
  `<th>塗布速度</th>` が残っており `<th>` は 10 個。期待は
  `1 + len(ResolvedSettings.model_fields)` = 1 + 8 = 9。→ `10 != 9` で **FAIL**。
- **修正後**: `<th>塗布速度</th>` を 1 行削除すると `<th>` は 9 個 → **PASS**。

## 実装側に求める修正（plan-implementer 向け）

- `src/webui/templates/partials/pad_editor.html` の thead から
  `<th>塗布速度</th>`（1 行）を削除する。これによりヘッダ `<th>` 数（9）が
  ボディ列数（ノード + 有効 + JS `FIELDS` 7 = 9）と一致する。
- テストは真の前提。`ResolvedSettings` / JS `FIELDS` 側は既に `fill_speed` 削除済みで
  追加変更は不要。

## tests/helpers.py への追加

- なし（fake Impl 不要。実サーバー E2E のみで完結）。

## 検証結果

- make format: **pass**（ruff / ruff-format / docformatter 等すべて Passed。docformatter が
  `_PadTableHeaderCounter` docstring の先頭を `Id=` に自動整形したが機能影響なし）。
- テスト実行: 未実行（venv 再構築中のため依頼どおりスキップ）。修正前 FAIL / 修正後 PASS を想定。
