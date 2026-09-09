---
name: バリデーションは値オブジェクトのメソッド
description: 値オブジェクトの検証は module-level 関数ではなくそのクラスのメソッドとして持つ
type: feedback
---

`attrs.frozen` の値オブジェクトが自身の整合を検証する場合、`validate_xxx(value)` という
module-level 関数ではなく、そのクラスの **`validate()` メソッド**として持つ。

```python
# 推奨
@attrs.frozen
class ImageConstraints:
    minimum_size: int = 32
    maximum_size: int = 1024

    def validate(self) -> str | None:
        if self.minimum_size < 1:
            return f"minimum_size は正の整数が必要です: {self.minimum_size}"
        return None

# 非推奨
def validate_image_constraints(constraints: ImageConstraints) -> str | None: ...
```

**Why:** 検証対象と検証規則が離れると、フィールドを足したときに検証の更新が漏れる。
呼び出し側も `value.validate()` の方が探しやすい。

**How to apply:**

- 戻り値は引き続き `str | None`（[try-catchより戻り値バリデーション](feedback_no_try_catch.md)）。
    例外は投げない
- 検証に外部の文脈が要る場合も、主対象のクラスのメソッドにして文脈を引数で受ける
    （例: `SplitManifest.validate(sample_groups, *, dataset_fingerprint) -> str | None`）
- 値オブジェクトを持たない検証（複数の独立した引数を突き合わせるだけのもの）は
    module-level 関数のままでよい
- 直近に触ったモジュール（`pasting/dataset/metadata.py`、`pasting/flowcalib/lines.py`、
    `pasting/testboard/config.py`）はこの規約に沿って移行済み
    （`DatasetView.validate()` / `LineLayout.validate()` / `BoardConfig.validate()`）
- それ以外の既存 `validate_*` は、その周辺を触る機会に合わせて移す。
    無関係なモジュールをこの規約のためだけに一括改名はしない（AGENTS.md 開発原則 3）
- スカラー 1 個を受ける検証（`config.py` の `validate_audio_volume` など）はメソッド化できない。
    `src/web/api/config_store.py` の `_coerce` がオブジェクト構築前に 1 フィールドだけ検証するため、
    module-level 関数のまま残す
