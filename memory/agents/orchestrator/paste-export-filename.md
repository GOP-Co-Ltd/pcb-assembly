# はんだ塗布 override 設定の書き出しファイル名

## 1. 計画

### 要求

書き出しファイル名を「基板名 + ISO datetime」にする。
現状は frontend の `pcbasm-paste-overrides-${Date.now()}.json`（epoch ミリ秒）。

### 現状

- 実際の保存名を決めているのは `src/web/ui/static/js/pad_editor/index.js` の
  export ハンドラ（blob + `link.download`）。backend の `Content-Disposition`
  （board_id = SHA-256 16 hex）は無視されている
- `downloadApi()`（`src/web/ui/static/js/app.js`）が既に `Content-Disposition`
  を読んで保存名にする。テスト塗布基板の書き出しで使用実績あり

### 決定

ユーザー確認: 形式は `<基板名>-paste-overrides-<YYYYMMDDTHHMMSS>.json`
（例 `led_blinker-paste-overrides-20260911T143005.json`）。
`%Y%m%dT%H%M%S` はリポジトリ既存の命名規約
（`paste_volume/calibration.py`・`pasting/dataset/writer.py`）に一致。

却下した案:
- `2026-09-11T14-30-05`（コロン置換の ISO 拡張形式）: 既存規約から外れる
- UTC オフセット付き: 単一装置・単一タイムゾーン運用で名前が伸びるだけ
- 用途語を落として `<基板名>-<日時>.json`: ダウンロードフォルダで判別できない

### 公開インターフェース

`src/pcbasm/pasting/persist.py`:

    def board_settings_export_filename(
        source_pcb: str, created_at: datetime | None = None
    ) -> str

ファイル名の組み立ては webui-thin-wrapper に従いサーバー側（pcbasm コア）に置く。
JSON の形の正典である persist に同居させる。

### 実装ステップ

1. `persist.board_settings_export_filename` を追加
2. `routers/pasting.export_pad_config` が使う。非 ASCII 基板名は
   `Content-Disposition` が latin-1 のため RFC 5987 `filename*` を併記する
   （併記しないと starlette が UnicodeEncodeError → 500）
3. JS の export ハンドラを `downloadApi` へ置換（自前 blob と Date.now を削除）

### テスト観点

- persist: 基板名が stem で入る／日時書式／`created_at` 省略時は現在時刻／
  ディレクトリ付き source_pcb／path に使えない文字を畳む／stem が空のとき既定名
- router: `Content-Disposition` に `led_blinker-paste-overrides-<日時>.json`
- router: 日本語基板名で 500 にならず `filename*=UTF-8''` が付く

### リスク

- `downloadApi` は `window.webui` 経由。pad_editor は現在 `{ api, toast }` だけを
  取り出しているので追加が必要

## 2〜3. テスト・実装

- `pcbasm.pasting.persist.board_settings_export_filename` を追加
  （unsafe 文字の畳み込みは ASCII 以外を残す = 日本語基板名を保つ）
- `routers/pasting._attachment` で RFC 5987 `filename*` を併記
- JS の export ハンドラを `downloadApi` へ置換（保存名をサーバーに一本化）

## 4. 自己レビュー（指摘と対応）

- `_EXPORT_STEM_FALLBACK` が 1 箇所使用の定数 → インライン化（開発原則 2）
- public な `board_settings_export_filename` が private helper 群の後ろ →
  `decode_board_settings` の直後へ移動
- `board_store.board_id` は保存パスの算出で現役。書き出し名から外れても未使用にならない

## 5. ドキュメント

- `persist.py` module docstring に書き出し名の正典であることを追記
- README / docs に旧ファイル名の記述なし

## 検証

- `make format && make type && make test-no-hardware`: 3464 passed
- `uv run pytest -m "e2e and not hardware" tests/e2e/test_paste_solder_browser.py`: 23 passed
  （実ブラウザの suggested_filename が `led_blinker-paste-overrides-<日時>.json`）
