---
description: MR3（src/webui → src/web/api の機械的リネーム）の実装ノート — 置換規則の線引き・判断が必要だった箇所・残存 webui の分類
---

# MR3: `src/webui` → `src/web/api` の機械的リネーム

振る舞い変更ゼロ。コミット 2 本（1 本目が `git mv` のみ、2 本目が文字列置換）。

## 置換規則の線引き（この 3 分類で全 `\bwebui\b` を裁いた）

MR3 の全数は 360 箇所（`grep -rn '\bwebui\b' src tests --include='*.py'`、移動前の計測）。

### A. 置換した（パッケージ実体を指すもの）

| 対象 | 規則 | 実測 |
| --- | --- | --- |
| モジュール参照 | `(?<!window\.)\bwebui\.` → `web.api.` | 247 |
| import 文 | 上の規則で `from webui...` / `import webui...` が `web.api` になる | 202 |
| `tests.webui.conftest` | 同じ規則で `tests.web.api.conftest` になる（`.` の直前が `\b` なので 1 発で当たる） | 2 |
| env prefix | `PCBASM_WEBUI_` → `PCBASM_API_` | settings.py 11 / test_settings.py 16 / Makefile 3 / README 2 / .claude skill 4 / .agents skill 4 |
| パス文字列 | `src/webui/` → `src/web/api/`、`tests/webui/` → `tests/web/api/`（ドキュメント・フック・テストパラメータ） | 下記参照 |
| make ターゲット | `make webui{,-dev,-fake}` → `make api{,-dev,-fake}`（ドキュメント側） | — |
| 起動コマンド | `python -m webui` → `python -m web.api` | — |

### B. 絶対に置換しなかった（データパスと外部識別子）

- `Settings.webui_data_dir` プロパティ名
- `webui_state.json`（`state.py` の `_STATE_FILENAME`）
- `self.data_dir / "webui"`（= `data/webui/`）と、その説明を書いた docstring 内の `data/webui/...`
- `data/testing/webui/fake_camera.png`（`settings.py` / `tests/web/api/conftest.py` /
  `tests/pcbasm/hal/test_framehub.py`）と `data/.gitignore` の `webui/` `webui_state.json`
- JS グローバル `window.webui`（`tests/e2e/test_browser_ui.py` の 2 箇所。置換規則に
  `(?<!window\.)` の negative lookbehind を入れて機械的に除外した。MR4 以降も
  `window.webui.withBase` / `window.webui.control` として使う予定）
- `webui-service.sh` / `pcbasm-webui.service` / `/tmp/pcbasm-webui-fake`（MR7 で扱う）
- skill ディレクトリ名 `webui-e2e` / `webui-thin-wrapper` と frontmatter の `name:`（MR7 で扱う）

### C. 意図して残した（歴史的文書への参照と、レイヤの通称）

1. **過去の計画書ファイル名とその見出しの引用。** `webui-phase{1..5}.md` /
   `webui-camera-calib.md` / `claude-webui-1-pad-extract-eager-pine.md` /
   `[[feedback-webui-claude-self-e2e]]` と、それらの中の節見出しを引用した
   `計画書「src/webui/jobs/manager.py」節が契約:` 形式の docstring。
   **書き換えると引用先が `memory/agents/**` の実ファイルに存在しない見出しになり、
   参照が辿れなくなる。** ブリーフの「`memory/agents/**` の過去ノートは触らない」と
   同じ理由でテスト側の引用も触らない。該当 24 ファイル・約 45 箇所。
2. **`pcbasm` 側 docstring の「webui」= Web 層の通称。** `src/pcbasm/posctrl/render.py`、
   `pasting/{dispense_calibration,fill_path,settings}.py`、および移動先パッケージ内の
   `jobs/{dev,pasting,posctrl}.py` の「KiCAD 未導入でも webui は起動可」コメント。
   モジュールパスではなく製品名（CLAUDE.md も「WebUI 設計」という節名を維持している）。
   ドメイン層のファイルを diff に引き込む価値がないので触らない。11 箇所。

## 判断が必要だった箇所

| # | 判断 | 理由 |
| - | ---- | ---- |
| 1 | `setup_logging(namespaces=("pcbasm", "webui"))` → `("pcbasm", "web")` | ブリーフの明示指示。ロガー名が `web.api.*` になるため `"web"` でないと backend のログが 1 行も出ない。namespace を assert するテストは無い（`tests/pcbasm/test_utils.py` は独自の namespace 名を使う） |
| 2 | `tests/web/api/test_models.py` の `roots.add("webui")` → `roots.add("web")` | 相対 import を表す sentinel。`models.py` のトップレベルパッケージが `web` になったので実態に合わせた。`allowed`（pydantic + stdlib）にどちらも含まれないので assert の結果は不変 |
| 3 | `tests/test_claude_hooks.py::TestHardwareMarkerLayout` の `"tests/webui/"` → `"tests/web/api/"` | **これは load-bearing。** 実ツリーを `rglob` して `@mark_hardware` の所在を確認するテストなので、直さないと必ず落ちる。同ファイルの BLOCKED / ALLOWED パラメータのパスも実在ディレクトリに揃えた（フック自体はパス存在を見ないので挙動不変） |
| 4 | `.claude/hooks/pretooluse-block-hardware-tests.py` の docstring と deny 理由メッセージのパス | 実機テスト禁止フックが「`tests/webui/` に `@mark_hardware` があります」と存在しないディレクトリを案内していた。判定ロジックは 1 行も変えていない（`diff` は 2 行のみ） |
| 5 | `src/web/api/models.py` の docstring `tests/webui/test_models.py` → `tests/web/api/test_models.py`、`pcbasm / webui の他モジュール` → `pcbasm / web.api の他モジュール` | 過去文書の引用ではなく現在のファイル・パッケージを指す記述 |
| 6 | `tests/web/api/jobs/conftest.py` の `fixture は tests/webui/conftest.py にある` → `tests/web/api/conftest.py` | 同上（現在のファイル参照） |
| 7 | `data/config-templates/README.md` の `src/webui/config_store.py` → `src/web/api/config_store.py` | ブリーフの列挙に無いが、`MACHINE_FIELDS` の所在を案内する運用ドキュメント。放置すると存在しないパスを指す |
| 8 | `Makefile` のエイリアスは prerequisite 方式（`webui: api`）にした | レシピを複製すると 2 箇所を保守することになる。`make -n webui` が `uv run python -m web.api` を出すことを確認済み |
| 9 | `api-fake` の `PCBASM_API_DATA_DIR` 既定 `/tmp/pcbasm-webui-fake` は改名しない | tmp の作業ディレクトリ名。`.claude/skills/webui-e2e` の記述と揃えたままにする（MR7 の整理対象） |
| 10 | `pyproject.toml` は触っていない | 計画書の指示どおり。`name = "pcbasm"` + `uv_build` なので `src/webui` も元から wheel に入っておらず、`.venv` の `pcbasm.pth`（`src/` を sys.path に載せる）で解決している。`src/web/__init__.py` を置いただけで `web.api` が import できる（実測: 全テスト緑） |

## ruff が自動整形した 1 箇所

`src/web/api/routers/app_state.py` の `from web.api.routers.common import ...` が
`webui.` → `web.api.` で 88 桁を超えたため括弧つき複数行に展開された。
**これが `make format` による唯一の非機械的変更**（HEAD の内容に置換規則を適用した
結果と worktree を突き合わせて全ファイル確認した）。

## `\bwebui\b` 残存の最終分類（`src` / `tests` の `*.py`）

```
データパス系（B）                              12 箇所
JS グローバル window.webui（B）                 2 箇所
過去計画書のファイル名・見出し引用（C1）       約 45 箇所
Web 層の通称としての散文（C2）                 11 箇所
```

`\bwebui\.`（モジュール参照形）の残存は `window.webui.jobs.currentJob()` の 2 箇所のみ。

## 既知の制約・残課題

- MR7 で扱う: skill ディレクトリ名 `webui-e2e` / `webui-thin-wrapper`、
  `webui-service.sh`、`pcbasm-webui.service`、`Makefile` の `webui*` エイリアス削除、
  `data/testing/webui/` と `data/webui/` は改名しない（board_id / 成果物の孤立を防ぐ）。
- `tests/e2e/test_webui_e2e.py` のファイル名は変えていない（ブリーフの対象外。
  MR4 で `live_ui` を入れるときに扱うのが自然）。
- `.gitlab-ci.yml` に `webui` の言及は 0 件だったので触っていない。
  `changes.paths` が `**/*.py` なので `src/web/api/` の Python 変更は従来どおり拾う
  （テンプレ・JS だけの変更が拾われない既存の穴は計画書どおり MR4 で塞ぐ）。

## 検証結果

- `make format`: pass（2 巡目は無変更）
- `make type`: pass（pyright 0 errors, 0 warnings）
- `make test-no-hardware`: pass（**1738 passed / 87 deselected** = MR2 と同数）
- `pytest -m "e2e and not browser and not hardware"`: 20 passed
- `make migrate-codex-check`: `.codex/rules/default.rules` は最新（`settings.json` 未変更）
- `make -n api` / `make -n webui` / `make -n webui-dev` / `make -n webui-fake`: すべて
  `web.api` を起動する形になっている（実サーバーは起動していない）
- `grep -rn '</content>' src tests`: 0 件
