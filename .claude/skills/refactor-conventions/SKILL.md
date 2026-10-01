---
name: refactor-conventions
description: PCB アセンブリのリファクタリング規約集。テスト方針の詳細（モック最小化、parametrize、テストクラス集約）、カプセル化（_ prefix）、私的属性を直接テストしない原則、try-catch ではなく None 返却バリデーションなど。コードを書き換える・テストを追加するときに参照する。
---

# リファクタリング規約

コードを書き換える・テストを追加するときに参照する規約集。
AGENTS.md の要点を補完し、詳細な判断基準と具体例を提供する。

## テスト方針詳細

### 何をテストするか

- **公開インターフェースの振る舞い**を入出力ベースで検証
- 正常系・異常系・警告（RuntimeWarning 等）・エッジケース

### 何をテストしないか

- 内部実装の詳細（特定メソッドが呼ばれたかなど）
- 初期化時の属性設定などの内部動作
- private（`_` prefix）の直接テスト → [memory/feedback_no_private_test.md](../../../memory/feedback_no_private_test.md) 参照

### テスト構造

- **クラスに集約する**（`class TestXxx:` 形式、関数単位ではない）→ [memory/feedback_test_class.md](../../../memory/feedback_test_class.md) 参照
- テスト関数に戻り値の型アノテーションは不要
- 複数パラメータは `@pytest.mark.parametrize` を使用

### モック方針

skill `testing-strategy` が単一の正典。ここでは要点だけを挙げる。

- fake してよいのは自前 HAL ABC（`src/pcbasm/hal/`）だけ
- 3rd-party 表面（`picamera2`、`libgpiod`、Klipper RPC、`cv2.*`、`time.sleep` 等）と内部 private 関数のモックは禁止。モックはテスト作成者の仮定を写すだけで、upstream の挙動変更を検出できないため
- 実オブジェクト＋実データを優先する。ABC に具象が無ければ `tests/helpers.py` に実 Impl を置く（`mocker.Mock` より先に検討）

### ハードウェアテスト分離

- `@mark_hardware` でハードウェアテストとモックテストを同一クラス内で分離
- 詳細は skill `hardware-test`

## カプセル化詳細

### `_` prefix 原則

- クラスの内部実装の詳細や属性は基本的にすべて private（`_` prefix）
- `__init__` で設定される属性は原則 private
- 外部から参照する必要がある属性のみ public

### 例

```python
class HeightPlane:
    def __init__(self, points: list[Point3d]):
        self._points = points  # private
        self._a, self._b, self._c = self._fit_plane(points)  # private

    def z_at(self, x: float, y: float) -> float:  # public API
        return self._a * x + self._b * y + self._c

    def _fit_plane(self, points: list[Point3d]) -> tuple[float, float, float]:
        # private helper
        ...
```

### public 化判断基準

- 外部呼び出し元が **本当に** 参照する必要がある属性のみ public 化
- 「テストから参照したい」は public 化の理由にならない（公開 API 経由でテストする）
- `pyright` の `reportPrivateUsage` warning が出る import は避ける

## エラーハンドリング

### try-catch より戻り値バリデーション

- 入力バリデーションでは try-catch ではなく **None 返却パターン** を使う
- 詳細は [memory/feedback_no_try_catch.md](../../../memory/feedback_no_try_catch.md) 参照

### 例

```python
# 推奨
def parse_position(s: str) -> Position | None:
    parts = s.split(",")
    if len(parts) != 3:
        return None
    try:
        return Position(*map(float, parts))
    except ValueError:
        return None

# 非推奨：呼び出し側にtry-catchを強いる
def parse_position(s: str) -> Position:
    return Position(*map(float, s.split(",")))
```

## リファクタリング技法

### 適用する典型パターン

- 不要なネストの平坦化（early return / guard clause）
- 不要な抽象化（1 箇所しか使わない関数/クラス）の解消
- 不適切な命名の改善（意図が読めない `data`, `tmp`, `helper` 等）
- wrapper / pass-through メソッドの除去
- 重複コードの抽出（ただし「3 度現れたら抽出」が目安、早すぎる抽象化を避ける）
- 巨大関数の分割（責務単位、ただし不要な分割は避ける）

### 適用しない（避ける）パターン

- 仮想的な将来要件のための抽象化
- 「綺麗に見える」だけの分割
- 公開 IF を変えるリファクタ（破壊的変更は別タスクで合意してから）

## 検証フロー

リファクタ・実装の完了条件は次のとおり。

```bash
make format && make type && make test-no-hardware
```

すべてパスして初めて完了。型チェックエラーや lint warning を放置しない。

`make test`（実機テストを含む）は実行しない。`settings.json` で deny 済みであり、実行すると実機が動く。実機確認はユーザーが行う。

## Git 運用との接続

- リファクタは `refactor/<日付>/<内容>` ブランチで作業
- コミットは `refactor(<スコープ>): <内容>` 形式
- 1 コミット 1 関心事、検証通過前のコミットは避ける
