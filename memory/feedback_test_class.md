---
name: テストはテストクラスにまとめる
description: pytestのテストはモジュール直下の関数ではなくclass TestXxx形式でまとめる
type: feedback
originSessionId: 7eb3c0b9-1064-471b-a027-b5cb11b5a829
---

pytestテストは関数ベースではなく `class TestXxx:` 形式でまとめる。同一対象の正常系・異常系・パラメータ化を1つのクラスに集約する。

**Why:** ユーザの好み。関連するテストをまとめて見やすくし、setup/teardownやfixtureスコープも整理しやすくするため。

**How to apply:**

- 新規テストファイルでは必ず `class TestTargetName:` を作り、その中にテストメソッドを書く
- 既存テストファイルを拡張する場合は、まず既存スタイルを確認し、クラスベースであれば既存クラスに追加。関数ベースであれば、新規追加分だけは新たなクラスにまとめる（既存の大規模リファクタリングは別タスク）
- パラメトライズは `@pytest.mark.parametrize` を各メソッドに付ける（クラス全体には付けない）
- テスト関数は `self` を取るメソッドとして定義
