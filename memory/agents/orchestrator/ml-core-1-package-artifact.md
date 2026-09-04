# コア ML 基盤 MR1: パッケージ骨格・依存グループ・artifact I/O

計画全体は `/home/gop/.claude/plans/mr185-codex-docs-image-based-dispense-ca-modular-sonnet.md`。
本ファイルは MR1 の作業記録。

## 段階 1: 計画

### 成果物

| 対象 | 内容 |
| --- | --- |
| `src/ml/__init__.py` | docstring のみ。re-export しない |
| `src/ml/artifact/atomic.py` | 唯一の atomic I/O 実装 |
| `src/ml/artifact/fingerprint.py` | canonical JSON と SHA-256 |
| `src/ml/artifact/document.py` | kind + schema_version エンベロープ |
| `src/ml/artifact/package.py` | 不変パッケージの publish / verify / active pointer / rollback |
| `src/ml/config/converter.py` | strict cattrs converter factory |
| `pyproject.toml` | `ml-runtime` / `ml-train` / `ml-hpo` / `ml-export` グループ、build-backend の module-name、torch index |
| `.gitlab-ci.yml` | `uv sync --locked --all-extras` → `--all-groups` |
| `tests/ml/` | architecture テストと上記モジュールのテスト |
| docs | `AGENTS.md` 主要構成、`docs/image-based-dispense-calibration-ml-plan.md` §7 と命名 |

### 公開インターフェース

```python
# ml/artifact/atomic.py
type StreamWriter = Callable[[BinaryIO], None]
type ReadbackValidator = Callable[[Path], None]

def atomic_write_stream(path: Path, write: StreamWriter, *,
                        validate_readback: ReadbackValidator | None = None) -> None
def atomic_write_bytes(path: Path, data: bytes) -> None
def atomic_write_text(path: Path, text: str) -> None
def atomic_write_json(path: Path, value: object) -> None

# ml/artifact/fingerprint.py
def canonical_json(value: object) -> str
def sha256_bytes(data: bytes) -> str        # 素の 64 桁 hex
def sha256_file(path: Path) -> str          # 素の 64 桁 hex
def fingerprint_json(value: object) -> str  # "sha256:" 付き

# ml/artifact/document.py
@attrs.frozen
class DocumentKind:
    kind: str
    schema_version: int

def unstructure_document(value: object, *, kind: DocumentKind,
                         converter: Converter) -> dict[str, object]
def structure_document[T](data: Mapping[str, object], target: type[T], *,
                          kind: DocumentKind, converter: Converter
                          ) -> tuple[T | None, str | None]
def save_document(path: Path, value: object, *, kind, converter) -> None
def load_document[T](path: Path, target: type[T], *, kind, converter
                     ) -> tuple[T | None, str | None]

# ml/artifact/package.py
CHECKSUM_FILENAME = "SHA256SUMS"

@attrs.frozen
class PublishedPackage:
    path: Path
    checksums: Mapping[str, str]

def publish_immutable_package(output_directory: Path, *,
                              payload_filenames: Iterable[str],
                              write_payloads: Callable[[Path], None]) -> PublishedPackage
def verify_immutable_package(package: Path, *, expected_filenames: Iterable[str] | None = None
                             ) -> tuple[PublishedPackage | None, str | None]

@attrs.frozen
class ActivePointer:
    pointer_path: Path
    active_package_path: Path
    previous_package_path: Path | None
    active_package_id: str
    active_package_sha256: str

def switch_active_pointer(pointer_file: Path, package: Path, *, package_id: str,
                          package_sha256: str) -> ActivePointer
def load_active_pointer(pointer_file: Path) -> tuple[ActivePointer | None, str | None]
def rollback_active_pointer(pointer_file: Path) -> tuple[ActivePointer | None, str | None]

# ml/config/converter.py
def make_strict_converter() -> Converter
```

### テスト観点

- atomic: 書き込み途中の失敗で temp を残さず既存ファイルを壊さない / readback 検証失敗で publish しない / 親ディレクトリ自動作成 / 既存ファイル置換 / JSON はキー整列と末尾改行、NaN 拒否
- fingerprint: canonical JSON がキー順に依存しない / NaN と Inf を拒否 / `fingerprint_json` の prefix / `sha256_file` が実ファイル内容と一致
- document: kind 不一致・schema_version 不一致で理由を返す / 未知キー拒否 / round-trip / int を float フィールドへ入れない
- package: publish が SHA256SUMS を作る / 既存ディレクトリを上書きしない / writer がファイル集合を違えたら失敗して temp を残さない / symlink 拒否 / verify が改竄を検出 / pointer 切替と 1 世代 rollback / pointer がパッケージ内部を指せない
- converter: bool/int/float/str の厳格性 / Literal / tuple / Path / forbid_extra_keys
- architecture: `src/ml/**` が `pcbasm` / `web` を import しない（相対 import を解決して判定）/ artifact 層が mlflow / hydra / optuna / onnx を読まない

### 計画からの補足・判断

1. **`atomic_torch_save` は MR1 に入れない。** MR1 を torch 非依存に保つため、
   汎用の `atomic_write_stream` だけを置き、torch checkpoint への適用は MR4
   (`ml/training/checkpoint.py`) で行う。全体計画の「atomic I/O は 1 実装」は維持される。
2. **`make_strict_converter()` に `allow_integer_for_float` フラグを今は付けない。**
   artifact wire は厳格でよい。config YAML で `1` を float に入れたい要求が出るのは MR5 なので、
   利用者が現れてから足す（AGENTS.md 開発原則 2）。
3. **`kind` / `schema_version` は attrs クラスのフィールドにしない。**
   `pcbasm.pasting.dataset.metadata.PasteDatasetMetadata` はフィールドとして持っているが、
   全 document 型で 2 フィールドを複製することになる。エンベロープとして
   `document.py` が外側で扱う。
4. **`hydra-optuna-sweeper` は `==1.4.0.dev9` を pin せざるを得ない。**
   PyPI の最新安定版 1.2.0 は `optuna<3.0.0` を要求し、optuna 4.x と両立しない
   （1.3/1.4 系は dev release しか存在しない）。全体計画の「stock sweeper を使う」判断
   （private API `_impl` を継承しない）は維持できるが、dev release 依存そのものは残る。
   MR5 で「sweeper を使わず Optuna を直接駆動する」案と比較し直す。
5. **`ml-export` から mlflow を外す。** MR185 は `ml-export` に mlflow を入れていたが、
   export と量子化に experiment tracking は不要。
6. **`module-name` に `web` も入れる。** MR185 は `["pcbasm", "ml", ...]` で `web` が
   欠けていた。現状 main も `[tool.uv.build-backend]` 自体が無く `web` は wheel に入らない。
   editable install が壊れないことを `uv sync` 後の import で確認する。

### リスク

- `module-name` を明示すると editable install の `.pth` 挙動が変わり `import web` が
  壊れる可能性。`uv sync` 後に import を実測する。
- `uv.lock` を pre-commit の uv 0.5.10 が書き換える一方ローカルは 0.12.5。torch index を
  足した後に lock が揺れないか確認する。

## 段階 2: テスト実装

`tests/ml/` に 89 テスト。red 確認は `ModuleNotFoundError: No module named 'ml'`。

計画から変えた点が 1 つある。`tests/ml/config/test_converter.py` は当初
`pytest.raises(ValueError, match=<field>)` で厳格性を検証する形で書いたが、cattrs は
`detailed_validation=True` のとき `ClassValidationError`（ExceptionGroup）を投げ、
フィールド名は `cattrs.transform_error()` を通さないと得られない。例外型に依存させる代わりに、
リポジトリ規約どおり理由文字列を返す公開関数 `structure_strictly()` を設けてそれを検証する形に
書き直した。検証している契約（「不正なフィールド名が失敗理由に現れる」）は変えていない。

## 段階 3: 機能実装

`make format && make type && make test-no-hardware` が green（2846 passed / 140 deselected）。

実装中の判断:

- `attrs.field(converter=dict)` は pyright が `dict` の overload から
  `Iterable[list[str]]` を推論して型エラーになる。シグネチャの明確な
  `_read_only_checksums()` を経由し、併せて `MappingProxyType` で本当に読み取り専用にした。
- 固定長 tuple (`tuple[int, int]`) は cattrs 既定だと unstructure しても tuple のまま残り、
  可変長 tuple (`tuple[int, ...]`) だけが list になる。JSON 表現が 2 通りになるのを避けるため
  unstructure hook factory で両方 list へ落とした。
- cattrs は Path・Literal・固定長 tuple の長さ不一致・未知キーを既定で正しく扱う。
  自前 hook は scalar 4 種（bool / int / float / str）の厳格照合だけに絞った。

## 段階 4: リファクタリングと自己レビュー

`git diff main...HEAD` を通読して 6 点直した。

1. **`ml.config.converter` → `ml.serialization` へ移設（計画からの逸脱）。**
   `ml.artifact.document` が converter を使うため、artifact → config という逆向きの依存が
   できていた。この converter は Hydra 設定専用ではなくワイヤ表現全般の変換なので、
   単独 module `ml/serialization.py` に置き直した。`ml/config/` は MR5 で Hydra 境界
   専用に作る。
2. `_fsync_directory` を `fsync_directory` として公開し、`publish_immutable_package` の
   rename 後にも呼ぶようにした。atomic 書き込みと同じ耐久性契約にそろえる。
3. `verify_immutable_package` が symlink だけでなく通常ファイル以外全般を拒否するようにした。
   payload 名のディレクトリがあると `sha256_file` が `IsADirectoryError` を送出して
   理由返却の契約を破っていた。
4. `_read_checksums` が `SHA256SUMS` の読み取り失敗（壊れたバイト列など）も理由として返す
   ようにした。
5. `switch_active_pointer` の docstring に、pointer の置き場所誤りは `ValueError`、
   パッケージ検証失敗は理由返却、という使い分けを明記した。
6. architecture テストの「走査範囲が空でないこと」を独立テストから本体テストの先頭 assert へ
   畳んだ。

3 と 4 は振る舞いの変更なので、テストを 2 本追加した
（`test_detects_a_payload_replaced_by_a_directory`、`test_reports_a_malformed_checksum_line`）。

意図的に採らなかった案:

- **パッケージ公開時に payload 1 つずつ fsync する。** 電源断でパッケージが不完全に
  残る可能性は残るが、`verify_immutable_package` が checksum で検出する。ディレクトリ木の
  fsync を書く複雑さに見合わないと判断した。rename 自体の永続化（上記 2）だけ行う。
- **`ActivePointer` と `_PointerRecord` の統合。** 4 フィールドが重複するが、pointer file の
  パスをその file 自身へ書き込むのは移動に弱い。ワイヤ型とドメイン型を分けたまま残す。
- **行長 88 文字への折り返し。** 既存 `src/` に 88 文字超が 1682 行あり、ruff は E501 を
  ignore している。日本語 docstring の折り返しはリポジトリの現状と合わないため揃えない。

## 段階 5: ドキュメント

- `AGENTS.md` の主要構成に `src/ml/` を追加し、依存の一方向性と dependency group の方針を
  1 段落で明記した。
- `docs/image-based-dispense-calibration-ml-plan.md` §7 の配置表を「基盤 = `ml.*` /
  ドメイン = `pcbasm.pasting.paste_volume.*`」へ書き換え、`ml` 側は torch を隠す関数内 import を
  しない方針を追記した。
- 同 doc の略語を綴りきった名前へ改めた（`hparams_search` → `hyperparameter_search`、
  `*.ckpt` → `*.pt`）。
