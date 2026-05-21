---
name: privateの直接テスト禁止
description: private関数/メソッド/属性 (_ prefix) は直接テストしない。公開APIの振る舞い経由で検証する
type: feedback
originSessionId: d51bbb12-c311-441c-8e60-90088d7e6499
---

private 関数・メソッド・属性 (`_` prefix) を直接テストしてはいけない。テストは公開 API の振る舞いを通じて行う。

**Why:** CLAUDE.md のテスト方針「内部実装の詳細はテストしない。公開インターフェースと振る舞いをテストする」に明示されている。private を直接テストすると、内部リファクタリング (関数の抽出/統合/シグネチャ変更) のたびにテストが壊れ、振る舞いを検証していないテストが固まってしまう。

**How to apply:**

- 新しい private ヘルパを追加した場合、その挙動は **公開 API のテストケースを増やして** 間接的に検証する。例: 配分ロジック `_allocate_per_island` は `sample_points_in_coppers` の「容量不足の島がある場合に余剰が再分配される」というケースとしてテストする
- private を import している既存テストを見つけたら指摘・修正対象 (公開API経由のテストに置き換える)
- pyright の `reportPrivateUsage` warning が出る import はテストでも避ける
