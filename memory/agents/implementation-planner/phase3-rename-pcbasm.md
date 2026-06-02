# Phase 3: パッケージ名リネーム `pcb_assembly` → `pcbasm`

## 概要

Python パッケージ（import 名）を `pcb_assembly` → `pcbasm` にリネームする。
ディレクトリ rename（`git mv`）+ import 文・文字列参照の一括書き換え + `pyproject.toml` の `[project].name` 変更。
内部ロジック・公開 API・関数シグネチャは一切変えない（名前空間 rename のみ）。Phase 4 の klipper 移動は本フェーズ対象外。

---

## 調査結果サマリ（確定事項）

### `pcb_assembly` 参照の全件分類（`.git`/`.venv` 除く）

| 種類 | 件数 | 場所 | 書き換え方式 |
|---|---|---|---|
| `from pcb_assembly... import` / `import pcb_assembly` | 約 178 行 | `src/pcb_assembly/`(24 files), `src/scripts/`(14 files), `tests/`(30 files) | sed 一括 |
| 文字列リテラル `"pcb_assembly..."` | 10 行 | 下記内訳 | sed 一括（同パターンで巻き込まれる） |
| doctest (`>>> from pcb_assembly`) | **0 件** | — | 対応不要 |
| `pyproject.toml` の `[project].name` | 1 行 | `pyproject.toml` L2 | **手動**（値が `pcb-assembly` ハイフン形式のため別扱い） |
| Makefile / *.sh / README / configs / data | **0 件** | — | 対応不要 |
| `src/klipper/` 内の `pcb_assembly` 参照 | **0 件** | — | 対応不要（Phase 4 で移動するが import 名参照なし） |

#### 文字列リテラル 10 件の内訳（sed `s/pcb_assembly/pcbasm/g` で正しく置換される）
- `src/pcb_assembly/utils.py:11` — `namespaces: Iterable[str] = ("__main__", "pcb_assembly")`（ロガー名前空間。`"pcbasm"` に置換必須。これを忘れるとリネーム後にログが出なくなる）
- `tests/conftest.py:16,29,46` — `mocker.patch("pcb_assembly.hal.camera...")`
- `tests/pcb_assembly/test_config.py:367,375` — `monkeypatch.setattr("pcb_assembly.config.PROJECT_ROOT", ...)`
- `tests/pcb_assembly/hal/test_camera.py:71` — `"pcb_assembly.hal.camera.get_camera_info"`
- `tests/pcb_assembly/posctrl/test_setup.py:30,86,97` — `mocker.patch("pcb_assembly.posctrl.setup...")`

#### `tests/test_package.py`（特別扱い・確認済み）
```python
import pcb_assembly
...
assert pcb_assembly.__version__ == pyproject["project"]["version"]
```
→ `import pcbasm` / `pcbasm.__version__` に置換（sed の通常パターンで巻き込まれる）。
このテストは version 整合をピンしている契約テスト。リネーム後も `pcbasm.__version__ == "0.1.0"` で通る（下記 `__init__.py` 解析参照）。

#### `src/pcb_assembly/__init__.py`（特別注意・コード変更不要）
```python
from importlib import metadata
__version__ = metadata.version(__name__.replace("_", "-"))
```
- 現状: `__name__ == "pcb_assembly"` → `.replace("_","-")` → `"pcb-assembly"` → `[project].name` と一致して解決。
- リネーム後: `__name__ == "pcbasm"` → `.replace("_","-")` → `"pcbasm"`（アンダースコアなしなので不変）→ 新 `[project].name = "pcbasm"` と一致。**コード変更不要**。ただし dist-info が `pcbasm-0.1.0.dist-info` として再インストールされていることが前提（→ 検証で `uv sync --reinstall` 必須の根拠）。

---

## uv_build 整合の結論（最重要）

### 仕組み（実測で確認済み）
1. **editable install は path ベース**: site-packages 内に `pcb_assembly.pth` が 1 行 `/home/gop/pcb-assembly/src` を持つだけ。Python は `src/` 配下で `__init__.py` を持つディレクトリを import 可能パッケージとして解決する。
2. `src/scripts/`・`src/klipper/` は `__init__.py` を**持たない**（implicit namespace / path import）。wheel build では**パッケージされない**（実測: `uv build --wheel` のホイール top-level は `pcb_assembly/` と dist-info のみ。`scripts/`・`klipper/` は含まれない）。
3. wheel のモジュール解決は `[project].name` 正規化（`pcb-assembly` → `pcb_assembly`）から導出。`src/<module>/` 名と一致する必要がある。`src/` に余分な `scripts`/`klipper` があってもビルドは成功する（実測でビルド成功確認済み）。

### rename への影響
- ディレクトリを `src/pcb_assembly/` → `src/pcbasm/` に rename し、`[project].name = "pcbasm"` にすると、正規化後モジュール名 `pcbasm` と `src/pcbasm/` が一致 → wheel build OK。
- **editable の `.pth` は `src/` を指すだけなので、ディレクトリ rename 直後でも `import pcbasm` は理屈上は通る**。ただし古い `pcb_assembly.pth` と `pcb_assembly-0.1.0.dist-info` が site-packages に残存し、`import pcb_assembly` も（src 配下に該当ディレクトリがなくなるので)失敗するが、`pcbasm.__version__` を解決する `metadata.version("pcbasm")` は**新しい dist-info が無いと失敗する**。

### reinstall 要否の結論
**`uv sync --reinstall` は必須**。理由:
1. `[project].name` を変えるので dist-info（`pcb_assembly-0.1.0.dist-info` → `pcbasm-0.1.0.dist-info`）と `.pth`（`pcb_assembly.pth` → `pcbasm.pth`）を貼り直さないと、`__init__.py` の `metadata.version("pcbasm")` が `PackageNotFoundError` になる。
2. 古い `pcb_assembly.pth` / dist-info が残ると `test_package.py` や import 解決が混乱する。
- `--reinstall` で旧 dist-info/.pth が掃除され、新名義で再生成される。

### `[tool.pyright].exclude` / `[build-system]`
- `[build-system]`（uv_build）は**維持**（確定事項。変更しない）。
- `[tool.pyright].exclude` の `./src/klipper` は**維持**（Phase 4 まで klipper はそのまま）。
- 既知の無害な警告: `warning: build_system.requires = ["uv-build>=0.9.7,<0.10.0"] does not contain the current uv version 0.10.9`。これは**既存の状態**であり Phase 3 の責務外。本フェーズでは触らない（全体計画の確定事項に従い build-system は維持）。

---

## sed 置換パターン（誤爆回避の根拠つき）

### 安全性の根拠（実測）
- `pcb_assembly` の直後の文字は **`.`(179回) / 空白(14回) / `"`(1回)** のいずれかのみ。**後続が単語構成文字（`[A-Za-z0-9_]`）になるケースはゼロ**（`pcb_assembly[A-Za-z0-9_]` の grep が空）。つまり `pcb_assembly` は常に完結したトークン。
- `.py` ファイル内に `pcb-assembly`（ハイフン形式）の文字列は**存在しない**。ハイフン形式は `pyproject.toml` の `[project].name` のみ（→ 手動編集）。
- よって `.py` に対しては単純全置換 `s/pcb_assembly/pcbasm/g` で誤爆しない。

### 実行コマンド（plan-implementer 用）
ディレクトリ rename を**先に**実行 → その後 sed。対象は `src/pcbasm/`, `src/scripts/`, `tests/`（rename 後のパス）。

```bash
# import 文・文字列リテラルの一括置換（.py のみ）
grep -rlZ "pcb_assembly" --include="*.py" src/pcbasm src/scripts tests \
  | xargs -0 sed -i 's/pcb_assembly/pcbasm/g'
```

### 手動確認が要る箇所（sed 後に目視 / grep で確認）
1. `pyproject.toml` L2 `name = "pcb-assembly"` → **手動で** `name = "pcbasm"` に変更（sed 対象外。ハイフン形式なので別作業）。
2. `src/pcbasm/utils.py` の `namespaces=("__main__", "pcbasm")` が置換されたか確認（ロガー名前空間。漏れるとログ抑止）。
3. `src/pcbasm/__init__.py` は**変更されないこと**を確認（`__name__.replace("_","-")` のまま正しい）。
4. 置換後に `pcb_assembly` 残存がないことを全リポジトリ grep で確認（memory/.claude/CLAUDE.md は履歴・ドキュメントなので Phase 3 では触らない=残存OK。下記検証参照）。

---

## 実装ステップ（plan-implementer 用・1 担当に集約）

> **担当分割の提案**: 本作業は機械的 rename のみで仕様変更なし。`src` + `scripts` + `tests` + `pyproject.toml` を **1 担当（plan-implementer 単独）に集約**してよい。spec-test-author を分離する必要はない（新規テストを書かない、公開 API も不変）。むしろ rename と import 書き換えはアトミックに行うべきで、分割するとビルド不能な中間状態が生まれるため**分割は不可**。

1. **ブランチ確認**: `refactor/20260527/phase3-rename-pcbasm`（既にチェックアウト済みのはず）。
2. **src 本体を git mv**:
   ```bash
   git mv src/pcb_assembly src/pcbasm
   ```
3. **tests を git mv**:
   ```bash
   git mv tests/pcb_assembly tests/pcbasm
   ```
4. **import / 文字列リテラルの一括置換**（上記 sed コマンド。対象: `src/pcbasm src/scripts tests`）。
5. **pyproject.toml を手動編集**: L2 `name = "pcb-assembly"` → `name = "pcbasm"`。`[build-system]` と `[tool.pyright].exclude` は触らない。
6. **残存チェック**: `grep -rn "pcb_assembly" src tests pyproject.toml`（→ 0 件であること）。
7. **editable 再インストール**: `uv sync --reinstall`（旧 `.pth`/dist-info を掃除し `pcbasm.pth` を生成）。
8. **検証**: `make run`（format → test → type）を全緑にする。
   - `make test` は `--doctest-modules` 込みで新パス `src/pcbasm/` を走査する（testpaths は `tests/` だが doctest-modules はインストール済みパッケージ経由で解決）。
9. **判断ログ**を `memory/agents/plan-implementer/phase3-rename-pcbasm.md` に残す（rename 実施・残存 0 確認・reinstall 実行の旨）。

---

## 検証チェックリスト

- [ ] `git mv src/pcb_assembly src/pcbasm` 実施（git が rename として追跡: `git status` で `renamed:`）
- [ ] `git mv tests/pcb_assembly tests/pcbasm` 実施
- [ ] sed 置換後 `grep -rn "pcb_assembly" src tests pyproject.toml` が **0 件**
- [ ] `src/pcbasm/utils.py` の `namespaces` が `"pcbasm"` に置換済み
- [ ] `src/pcbasm/__init__.py` は無変更（`__name__.replace("_","-")` のまま）
- [ ] `pyproject.toml` の `name = "pcbasm"`、`[build-system]` 維持、`[tool.pyright].exclude` の `./src/klipper` 維持
- [ ] `uv sync --reinstall` 実行 → site-packages に `pcbasm.pth` が生成・`pcb_assembly.pth`/`pcb_assembly-*.dist-info` が消失
- [ ] `uv run python -c "import pcbasm; print(pcbasm.__version__)"` → `0.1.0`
- [ ] `uv run python -c "import scripts"` 相当（scripts が pcbasm を import 解決できる）
- [ ] `make format` パス（ruff isort で import 並びが整う）
- [ ] `make type` パス（pyright、klipper は exclude 維持）
- [ ] `make test` パス（`test_package.py` の version ピン含む、doctest-modules 含む）

---

## 想定リスク・トレードオフ

1. **reinstall 漏れ**（最大リスク）: ディレクトリ rename だけして `uv sync --reinstall` を忘れると、`metadata.version("pcbasm")` が `PackageNotFoundError` になり `import pcbasm` が `__init__` で即死、全テストが collection error。→ ステップ 7 を必須化、チェックリストで明示。
2. **古い dist-info/.pth 残存**: `--reinstall` なしの `uv sync` だと旧 `pcb_assembly.pth` が残り、`import pcb_assembly`（src 配下から消えている）も `import pcbasm`（dist-info 未生成）も中途半端。→ `--reinstall` で解決。
3. **sed 誤爆**: 実測で「`pcb_assembly` は常に完結トークン・`.py` 内にハイフン形式なし」を確認済みのため、リスクは実質ゼロ。ただし sed 対象から `pyproject.toml` を**外す**こと（ハイフン形式で別途手動）。
4. **utils.py のロガー名前空間漏れ**: sed で巻き込まれるが、見落とすとリネーム後ログが出ない silent failure。チェックリストで明示確認。
5. **git mv vs 削除＋追加**: `git mv` でディレクトリごと移動すると履歴が rename として追跡される。手動 `mv` + `git add` でも結果は同じだが `git mv` 推奨。
6. **build-system 警告**: `uv-build<0.10.0` と uv 0.10.9 の不一致警告は既存の状態。Phase 3 の確定事項により build-system は維持するため**無視**（本フェーズで pin を上げない）。
7. **memory/.claude/CLAUDE.md 内の `pcb_assembly` 文字列**: ドキュメント・履歴メモなので Phase 3 のコード rename スコープ外。残存していてもビルド/テストに影響しない。本フェーズでは触らない（残存 grep のスコープを `src tests pyproject.toml` に限定する根拠）。

---

## 参照

- 全体計画: `/home/gop/.claude/plans/claude-src-scripts-paste-solder-py-recursive-candle.md`
- 規約: skill `refactor-conventions`（外科的変更）, CLAUDE.md（カプセル化・Git 運用）
- 実装者引継ぎ: `.claude/agents/plan-implementer.md`
- 関連: `src/pcbasm/__init__.py`（version 解決ロジック）, `src/pcbasm/utils.py`（ロガー名前空間）, `tests/test_package.py`（version 契約テスト）, `pyproject.toml`（`[build-system]` uv_build / `[project].name`）
