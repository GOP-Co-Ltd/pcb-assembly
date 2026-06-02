# Phase 1: scripts 整理 + paste_solder.py 関数化

## 概要

`src/scripts/` 直下に平らに並ぶ 15 本の実行スクリプトを役割別サブディレクトリ
（`posctrl/ pasting/ pnp/ dev/`）に振り分け、`paste_solder.py` の `main()` を
3 つの private 関数（環境構築 / 高さ計測 / 塗布実行）に分解する。
パッケージ構造（`pcb_assembly.*`）は一切触らない。`main()` の実機挙動は完全に等価に保つ。

ブランチ: `refactor/20260527/phase1-scripts-reorg`（base: main、作業中）。

---

## 調査結果サマリ（着手前に必ず読む）

### A. import / パス参照は移動で壊れるか → 基本「壊れない」。例外は test 1 本のみ

- **全スクリプトの `pcb_assembly.*` import は絶対 import**。`pcb_assembly` は editable
  install 済み（`.venv` 経由、`src/` が sys.path 上）。スクリプトのファイル位置に依存しないので
  サブディレクトリ移動の影響を受けない。**書き換え不要。**
- **スクリプト間の相互 import は存在しない**（`from scripts...` / sibling import なし）。
- **`sys.path` 操作・`__file__` 相対パス参照はスクリプト内に一切無い**
  （`grep -rn "__file__\|sys.path" src/scripts/` → 0 件）。
- `PROJECT_ROOT` を使うスクリプト（camera_calibration / circle_detection_demo /
  pasting_height_plane / pasting_toolhead_offset）はあるが、`PROJECT_ROOT` の定義は
  `src/pcb_assembly/utils.py` 内の `Path(__file__).parent.parent.parent` で、**utils.py の
  位置を基準に解決される**。スクリプト側の移動では値が変わらない。**安全。**
- 外部設定・ドキュメント・シェルスクリプト・Makefile からのスクリプトパス参照は **無し**
  （`grep -rn "src/scripts/" --include=Makefile --include=*.sh --include=*.toml --include=*.md`
  でヒットするのは過去のエージェントメモ `memory/agents/**` のみ＝履歴、修正不要）。

### B. 唯一の破壊ポイント: `tests/scripts/test_generate_grid_pcb.py`

このテストは `generate_grid_pcb.py` を **module import** で参照している:

```python
from scripts.generate_grid_pcb import generate_grid_pcb   # 計 8 箇所
from scripts.generate_grid_pcb import main                 # 2 箇所
# さらに sys.modules キャッシュ削除キー:
if "scripts.generate_grid_pcb" in sys.modules:
    del sys.modules["scripts.generate_grid_pcb"]            # 1 箇所
```

`generate_grid_pcb.py` を `dev/` に移すと、import パスは
`scripts.dev.generate_grid_pcb` に変わる。`scripts` は `__init__.py` を持たない
**namespace package** であり、`scripts.dev` も namespace sub-package として
`__init__.py` 無しで自動解決される（`importlib.util.find_spec('scripts')` で実証済み、
`python -m scripts.dev.generate_grid_pcb` も動く想定）。
→ **テスト側の import 文 10 箇所 + sys.modules キー 1 箇所を `scripts.dev.generate_grid_pcb`
に書き換える必要がある。これは `tests/` 配下なので spec-test-author の担当**
（plan-implementer は `src/` のみ。tests には触れない）。
**Phase 1 では plan-implementer と spec-test-author の役割が分かれる点に注意。**

### C. 起動方法（移動後）

両スタイルとも現状動作を確認済み。移動後の正しい起動:
- module 形式: `uv run python -m scripts.<sub>.<name>`
  例 `uv run python -m scripts.pasting.paste_solder ...`
- file-path 形式: `python src/scripts/<sub>/<name>.py ...`
  （`sys.path[0]` がスクリプト自身のディレクトリになるが、sibling import が無いので無害）

実機運用コマンド（全体計画の実機検証節）は移動後こうなる:
```bash
python src/scripts/pasting/paste_solder.py --machine pd_china_frame \
  --pcb-file data/<本番 PCB>.kicad_pcb --tolerance 0.1
```

### D. テスト / 型チェックへの影響

- `--doctest-modules` は `testpaths = "tests/"` に対してのみ走り、`src/scripts/` は対象外。
  scripts 内に doctest（`>>> `）は 0 件。doctest 起因の破壊なし。
- `make type`（pyright）は `src/` 全体を見る。paste_solder.py 分解後の型注釈漏れが
  あれば検出される（後述の検証で担保）。
- `make test-no-hardware` で test_generate_grid_pcb.py の import 解決をチェックできる
  （pcbnew をモックするので非ハードウェア）。**移動 + test 書き換えが同期していないと
  collection error で落ちる** → 検証の要。

---

## ファイル移動の完全対応表（git mv）

サブディレクトリには **`__init__.py` を置かない**（namespace package のまま）。
移動先ディレクトリは `git mv` 実行前に `mkdir -p` で作成する。`pnp/` は空のまま予約。

```bash
# 0. 移動先ディレクトリを作成（pnp は空だが mkdir のみ。git は空ディレクトリを追跡しない点に注意）
mkdir -p src/scripts/posctrl src/scripts/pasting src/scripts/pnp src/scripts/dev

# 1. posctrl/
git mv src/scripts/board_tour.py            src/scripts/posctrl/board_tour.py
git mv src/scripts/camera_calibration.py    src/scripts/posctrl/camera_calibration.py
git mv src/scripts/camera_preview.py        src/scripts/posctrl/camera_preview.py
git mv src/scripts/circle_detection_demo.py src/scripts/posctrl/circle_detection_demo.py
git mv src/scripts/orthogonality_test.py    src/scripts/posctrl/orthogonality_test.py

# 2. pasting/
git mv src/scripts/paste_solder.py           src/scripts/pasting/paste_solder.py
git mv src/scripts/paste_flow_calibration.py src/scripts/pasting/paste_flow_calibration.py
git mv src/scripts/paste_loading.py          src/scripts/pasting/paste_loading.py
git mv src/scripts/pasting_toolhead_offset.py src/scripts/pasting/pasting_toolhead_offset.py
git mv src/scripts/pasting_height_plane.py   src/scripts/pasting/pasting_height_plane.py

# 3. dev/
git mv src/scripts/extract_pcb.py        src/scripts/dev/extract_pcb.py
git mv src/scripts/fill_path_simulate.py src/scripts/dev/fill_path_simulate.py
git mv src/scripts/generate_grid_pcb.py  src/scripts/dev/generate_grid_pcb.py
git mv src/scripts/klipper_demo.py       src/scripts/dev/klipper_demo.py
git mv src/scripts/stage_demo.py         src/scripts/dev/stage_demo.py

# 4. pnp/ は空（プレースホルダ）。git は空ディレクトリを追跡しないため、
#    Phase 1 では「pnp/ ディレクトリが git 上に出現しなくても許容」する。
#    全体計画でも src/scripts/pnp は「（空）」指定なので __init__.py も .gitkeep も置かない。
#    → mkdir はローカル作業上の任意。コミット対象にならなくて良い。
```

実行後の確認:
```bash
ls src/scripts/        # posctrl/ pasting/ dev/ のみ（直下に *.py が残っていないこと）
ls src/scripts/*.py 2>/dev/null && echo "NG: 直下に py が残存" || echo "OK"
```

> 注意: `__pycache__` は移動対象外。`git mv` は追跡ファイルのみ動かすため `__pycache__` は
> 旧位置に残るが git 管理外なので無視してよい（気になれば `make clean` で掃除）。

---

## paste_solder.py 分解の設計

移動先 `src/scripts/pasting/paste_solder.py` 内で完結。**パッケージへの抽出はしない**（YAGNI）。
import 文は現状維持（`from pcb_assembly...` のまま、追加で `attrs` を import）。

### フェーズ間を跨ぐ変数の分析（現 main() 精読結果）

| 変数 | 生成位置 | 後続で使う先 | Env に入れるか |
|---|---|---|---|
| `klipper` | result.klipper | measure / session / applicator / loading / 緊急停止 | ✔ |
| `stage` | result.stage | measure / sort / applicator / loading | ✔ |
| `machine` | result.machine（再代入） | dispenser_config / probe_config の供給元 | ✔ |
| `board_transform` | result.board_transform | measure の board_to_machine / 全変換 transform | ✔ |
| `toolhead_offset` | `machine.paste_dispenser.toolhead.to_transform()` | measure / 全変換 | ✔ |
| `probe_executor` → `height_measurer` | machine.probe から構築 | `_measure_height` 専用 | ✔（measurer を入れる） |
| `paste_dispenser` | machine.paste_dispenser から構築 | `_load_and_apply` 専用 | ✔ |
| `dispenser_config` | machine.paste_dispenser | applicator 構築 | machine 経由で再取得可（Env に machine があるので不要） |
| `top_coppers` | result.pcb から（TOP copper） | `_measure_height` 引数 | Env 外（引数で渡す＝計画の指定通り） |
| `top_pads` | result.pcb から（TOP pad） | `_load_and_apply` 引数 | Env 外（引数で渡す） |
| `height_plane` | `_measure_height` の戻り | `_load_and_apply` 引数 | フェーズ間受け渡し（戻り値→引数） |

**判断**: `top_coppers` / `top_pads` は計画書の関数シグネチャ（`_measure_height(env, top_coppers)`,
`_load_and_apply(env, height_plane, top_pads, args)`）で明示的に引数化されているので Env には入れない。
`toolhead_offset` は measure・全変換の両方で使い、`machine.paste_dispenser.toolhead.to_transform()`
の再評価で同値が得られるが、**現 main では 1 度だけ計算して使い回している**。挙動等価を厳密に保つため
Env に保持して使い回す（再計算しても結果は同じだが、副作用フリーな pure 変換なので等価性は保たれる。
保持の方が読みやすく diff も追える）。

### Env の定義（attrs.frozen）

```python
@attrs.frozen
class Env:
    """paste_solder の各フェーズが共有する初期化済みオブジェクト群."""

    machine: Machine
    klipper: Klipper
    stage: XYZStage
    board_transform: Transform
    toolhead_offset: Transform
    height_measurer: HeightPlaneMeasurer
    paste_dispenser: PasteDispenser
```

> 型注釈に必要な追加 import: `from pcb_assembly.config import Machine`(※実際の Machine 型の
> import 元は要確認。`get_machine_config` の戻り型に合わせる)、
> `from pcb_assembly.hal import Klipper, XYZStage`、`from pcb_assembly.geometry import Transform`。
> plan-implementer は実装時に `get_machine_config` / `BoardCalibrationResult` の型定義を見て
> 正確な import パスを確定すること（`Machine` は `pcb_assembly.config`、`Transform` は
> `pcb_assembly.geometry`、`XYZStage`/`Klipper`/`PasteDispenser`/`Probe` は `pcb_assembly.hal`）。

### 公開インターフェース案（private 関数 3 本 + main）

現 main() L66〜109 が `_setup_environment`、L114〜119 が `_measure_height`、
L121〜164 が `_load_and_apply` に対応する。

```python
def _setup_environment(args: argparse.Namespace) -> tuple[Env, BoardCalibrationResult]:
    """klipper/stage/machine/board_transform/toolhead_offset/height_measurer/dispenser を構築して返す.

    現 main() の L68〜109 を移設。
    machine_session の「外」で行う初期化に相当する（session に入る前に呼ぶ）。
    top_coppers / top_pads は result.pcb から生成するため、Env と一緒に
    BoardCalibrationResult も返して呼び出し側で pcb を参照させる。
    """
```

> 設計判断: `top_coppers` / `top_pads` は `result.pcb` から生成する。Env にこれらを入れない
> という計画指定を守るには、main() 側で result.pcb にアクセスできる必要がある。
> 選択肢:
> - (a) `_setup_environment` が `(Env, BoardCalibrationResult)` を返し、main で
>   `result.pcb` から top_coppers/top_pads を作る ← **推奨**。pcb 取得ロジックが main に残り、
>   `_measure_height`/`_load_and_apply` の引数（top_coppers/top_pads）と計画シグネチャが一致する。
> - (b) `_setup_environment` が `(Env, top_coppers, top_pads)` を返す ← main がスリムになるが
>   計画の「`_setup_environment(args)` → ... を集約して返す」記述から逸脱。pcb フィルタは
>   環境構築でなく「対象データ抽出」なので責務的にも main 寄りが自然。
>
> → (a) を採用。`top_coppers`/`top_pads` の生成（現 L82-83）は main に置く。

```python
def _measure_height(env: Env, top_coppers: list[Copper]) -> HeightPlane:
    """height_measurer.measure() を実行して HeightPlane を返す.

    現 main() の L115〜119（"=== Height plane計測 ===" print 含む）を移設。
    board_to_machine = Compose([env.board_transform, env.toolhead_offset]).
    machine_session の「内」で呼ばれる前提（呼び出しコンテキストは main が握る）。
    """


def _load_and_apply(
    env: Env,
    height_plane: HeightPlane,
    top_pads: list[Pad],
    args: argparse.Namespace,
) -> None:
    """nearest ソート → PasteApplicator 構築 → 任意ローディング → リトラクション → 塗布.

    現 main() の L121〜164 を移設（"=== リトラクション ===" / "=== パッド塗布 ===" print 含む）。
    transform = Compose([env.board_transform, env.toolhead_offset, height_plane]).
    PasteApplicator の構築は env.machine.paste_dispenser（dispenser_config）から行う。
    args は --interactive-loading / --amount を参照する。
    """


def main() -> None:
    """argparse → machine_session → _measure_height / _load_and_apply の組み立て.

    現 main() の構造を保つ:
      args = parser.parse_args()
      env, result = _setup_environment(args)
      top_coppers = [c for c in result.pcb.copper if c.layer == Layer.TOP]
      top_pads    = [p for p in result.pcb.pads  if p.layer == Layer.TOP]
      with machine_session(env.klipper):
          try:
              height_plane = _measure_height(env, top_coppers)
              _load_and_apply(env, height_plane, top_pads, args)
          except KeyboardInterrupt:
              print("\n=== 緊急停止 ===")
              env.klipper.emergency_stop()
    """
```

> `Pad` / `Copper` の正確な型名・import 元は plan-implementer が `pcb_assembly.pcb` の
> 公開シンボルを見て確定する（`result.pcb.copper` / `result.pcb.pads` の要素型）。
> 型が即座に確定できない場合、計画の挙動等価を最優先し、注釈は実在シンボルに合わせる
> （`list[Copper]` 等が import できなければ要素型をそのまま使う）。**型名のために挙動を変えない。**

### 挙動等価の厳守ポイント

- print 文の文言・出力順・改行（`"\n=== ... ==="`）を**一字一句変えない**。
- `machine_session` に入る前 / 後の処理境界を現状と完全一致させる
  （初期化は session 外、計測・塗布は session 内、KeyboardInterrupt は session 内 try で捕捉）。
- `Compose([...])` の合成順を変えない（measure 用は `[board_transform, toolhead_offset]`、
  塗布用は `[board_transform, toolhead_offset, height_plane]`）。
- `WINDOW_NAME` 定数は維持。`setup_logging(logging.INFO)` の位置（main 冒頭）も維持。
- argparse の引数定義（`--machine/-m`, `--pcb-file/-p`, `--tolerance/-t`, `--amount`,
  `--interactive-loading/-l`）とデフォルト値・help 文言を変えない。

---

## 実装ステップ（plan-implementer 用、検証込み）

> 前提: ブランチ `refactor/20260527/phase1-scripts-reorg` 上で作業（既に作業中）。
> plan-implementer は `src/` のみ編集。tests への変更は **spec-test-author** に委譲する
> （ステップ 4 参照）。

1. **ディレクトリ移動（git mv）** — 上記「完全対応表」のコマンドを順に実行。
   - 検証: `ls src/scripts/*.py 2>/dev/null && echo NG || echo OK`（直下に py が残っていない）
   - 検証: `git status` で 15 ファイルが rename 検出されていること

2. **paste_solder.py 分解** — `src/scripts/pasting/paste_solder.py` を編集。
   - `import attrs` を追加し、`Env`（attrs.frozen）を定義。
   - 型注釈用 import（Machine/Transform/Klipper/XYZStage/PasteDispenser/HeightPlaneMeasurer/
     Copper/Pad/HeightPlane）を実在シンボルに合わせて追加。
   - `_setup_environment` / `_measure_height` / `_load_and_apply` を新設し、
     現 main() の対応ブロックを移設。
   - main() を組み立て専念形に書き換え（上記擬似コード通り）。
   - 検証: `make type`（pyright）で paste_solder.py に型エラーが出ないこと。

3. **静的検証**
   - `make format`（ruff/docformatter）
   - `make type`（pyright、`src/` 全体）
   - 期待: 移動した他スクリプトでも import エラー 0（絶対 import なので元々壊れない）。

4. **テスト追従の委譲（spec-test-author 領域、src/ 担当者は触らない）**
   - `tests/scripts/test_generate_grid_pcb.py` の
     `from scripts.generate_grid_pcb import ...`（10 箇所）と
     `sys.modules["scripts.generate_grid_pcb"]`（1 箇所）を
     `scripts.dev.generate_grid_pcb` へ書き換える。
   - **挙動・アサーションは変えない**（パス追従のみ）。テストクラス構成も維持。

5. **統合検証（ステップ 2・4 が揃ってから）**
   - `make test-no-hardware`
     - 期待: collection error なし。`tests/scripts/test_generate_grid_pcb.py` の全テストが
       新パスで import 解決して pass。
   - `make test`（hardware 含む。ハードウェア未接続なら `@mark_hardware` は自動 skip/deselect）
     — Phase 1 終端でユーザー判断（実機テストは Claude main では実行しない）。

6. **移動後起動の手動確認（任意・非ハードウェア）**
   - `uv run python -m scripts.dev.generate_grid_pcb --help`
   - `uv run python src/scripts/pasting/paste_solder.py --help`
     （argparse usage が出ることのみ確認。実機接続は不要＝--help は parse 前に exit）

---

## テスト観点

- **正常系**:
  - `make test-no-hardware` が全 pass（特に test_generate_grid_pcb の 8 関数）。
  - `make type` が paste_solder.py 分解後もエラー 0。
  - 移動した 14 スクリプト（paste_solder 以外）が import レベルで壊れていない
    → pyright が `src/scripts/**` を走査して未解決 import を出さないことで担保。
- **異常系**:
  - test 追従漏れ（ステップ 4 未実施）の場合 `make test-no-hardware` が
    `ModuleNotFoundError: No module named 'scripts.generate_grid_pcb'` で collection error
    になる → これが検出されれば「移動と test 書き換えの同期漏れ」を正しく捕捉できている。
  - paste_solder.py の Compose 合成順・print 文言を変えると振る舞いが変わるが、
    paste_solder には自動テストが無い（実機運用スクリプト）。**コードレビューで diff を
    1 行ずつ現 main() と突き合わせて等価性を目視確認する**ことが唯一の防御線。
- **エッジケース**:
  - `pnp/` は空のため git にコミットされない（追跡ファイルなし）。これは計画想定内で
    エラーではない。「pnp/ が git 上に無い」ことを異常と誤判定しないこと。
  - `python -m scripts.dev.<name>` が namespace package として解決すること
    （`scripts` も `scripts.dev` も `__init__.py` 無しで OK）。

---

## 想定リスク・トレードオフ

| リスク | 影響 | 対策 |
|---|---|---|
| test_generate_grid_pcb の import パス追従漏れ | `make test-no-hardware` が collection error | ステップ 4 を spec-test-author に明示委譲。統合検証（ステップ 5）で必ず捕捉される |
| plan-implementer が tests を触れない制約と、test 追従が必要な事実の競合 | 役割境界の混乱 | **Phase 1 は plan-implementer（src 移動+分解）と spec-test-author（test の import 追従）の 2 者が必要**。全体計画の「Phase 1: planner → implementer → simplifier → docs-keeper」に test 担当が明記されていないが、generate_grid_pcb 移動に伴い spec-test-author の最小介入が不可避。Claude main はこの 1 点を追加手配すること |
| paste_solder.py の挙動非等価（print 順・Compose 順・session 境界） | 実機運用で挙動変化 | 自動テスト無し → diff の行単位レビューで担保。本ドキュメントの「挙動等価の厳守ポイント」をレビュー チェックリスト化 |
| `Env` の型注釈 import が確定しづらい | pyright エラー or 注釈過剰 | 型のために挙動を変えない。import 元が即断できなければ実在シンボルに合わせる。最悪 `Env` のフィールド型は構築元（result.* / get_machine_config）の戻り型に従う |
| `__pycache__` が旧位置に残る | 無害（git 管理外） | 任意で `make clean` |
| `pnp/` 空ディレクトリが git に残らない | 構造が「見えない」 | 計画想定内。`.gitkeep` も `__init__.py` も置かない（計画指定）。Phase 4 以降や PnP 着手時にファイルが入って自然に出現する |

### トレードオフの明示（判断を仰ぐ点）

- **`top_coppers`/`top_pads` 生成位置**: main に残す案 (a) を推奨採用したが、
  「main をさらにスリムにしたい」なら `_setup_environment` が返す選択肢 (b) もある。
  ただし (b) は計画の関数シグネチャ（`_measure_height(env, top_coppers)`）と矛盾するため
  (a) を採る。異論があれば Phase 1 着手前に確定したい。
- **`toolhead_offset` を Env に保持 vs 各所で再計算**: 保持を採用（現 main が 1 回計算して
  使い回しているため等価性が最も明確）。再計算でも pure 変換なので結果同値だが、保持の方が
  「現状の挙動をそのまま写経した」ことが diff で読み取れる。

---

## 参照

- 全体計画: `/home/gop/.claude/plans/claude-src-scripts-paste-solder-py-recursive-candle.md`（Phase 1 = L81〜93）
- 分解対象: `/home/gop/pcb-assembly/src/scripts/paste_solder.py`（現 main: L30〜172）
- 影響テスト: `/home/gop/pcb-assembly/tests/scripts/test_generate_grid_pcb.py`
- 戻り型: `BoardCalibrationResult` = `src/pcb_assembly/control/setup.py` L65〜76
  （machine/klipper/stage/camera/calibration/offset_transform/board_transform/pcb）
- `HeightPlaneMeasurer.measure(coppers, board_to_machine) -> HeightPlane` = `control/adjust/height.py` L94
- `PasteApplicator`（`__enter__`/`__exit__`/`retract`/`apply`）= `control/pasting/applicator.py`
- pytest 設定: `pyproject.toml` L38〜57（`testpaths="tests/"`, `--doctest-modules`, `--strict-markers`）
- 規約: skill `refactor-conventions`（private は `_` prefix・外科的変更）、`testing-strategy`（tests ミラー）
- 検証コマンド: `Makefile`（`test` / `test-no-hardware` / `type` / `format` / `run`）
