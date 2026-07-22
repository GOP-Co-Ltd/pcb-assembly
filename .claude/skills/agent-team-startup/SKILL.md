---
name: agent-team-startup
description: orchestrator（メインエージェント）が implementation-planner →（任意 spec-test-author）→ plan-implementer → code-reviewer ⇄ code-simplifier → docs-keeper を統括する標準フロー、モデル構成（判断系 inherit / 実装系 sonnet）、並列化の判断基準。ユーザーが「エージェントチームで進めて」と言ったり、複数モジュールにまたがる中〜大規模変更を要求したときに参照する。
---

# エージェントチーム起動手順

ユーザーから「エージェントチームで」と指示があった場合、または中〜大規模な変更（複数モジュールにまたがる実装・リファクタリング）を行う場合の進行手順。

## 前提

- `settings.json` に `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` が設定済み（必須）
- メインエージェントは `orchestrator`（`settings.json` の `"agent": "orchestrator"` で設定済み。`claude --agent orchestrator` でも起動可）
- 各 agent は `.claude/agents/` 配下に定義済み（orchestrator, implementation-planner, spec-test-author, plan-implementer, code-reviewer, code-simplifier, docs-keeper）
- 中間メモは `memory/agents/<agent-name>/<task-slug>.md` に書く（詳細は `memory/agents/README.md`）

## 役割とモデル構成

判断系（統括・計画・レビュー）はセッションモデルを継承（inherit）し、実装系は sonnet で回す。呼び出し時に Agent tool の `model` パラメータで一時的に上書きもできる（frontmatter より優先）。

| agent                  | model   | 役割                                                             |
| ---------------------- | ------- | ---------------------------------------------------------------- |
| orchestrator           | inherit | メインエージェント。統括・委譲・レビュー裁定・合流検証           |
| implementation-planner | inherit | 要件の仕様化・計画書作成（コードは書かない）                     |
| code-reviewer          | inherit | レビュー（仕様準拠・バグ・規約・テスト品質）。コードは編集しない |
| plan-implementer       | sonnet  | 計画に基づく実装・テスト・グリーン化                             |
| spec-test-author       | sonnet  | 仕様テスト（`tests/` のみ、`src/` は触らない）                   |
| code-simplifier        | sonnet  | 公開IF維持の簡素化・リファクタリング（レビューはしない）         |
| docs-keeper            | sonnet  | README/docstring の最小保守                                      |

## 標準サイクル

```
0. orchestrator（メイン）
   - 要件確認・ブランチ作成・以降の統括。src/ tests/ は自分で編集しない

1. implementation-planner
   - 要件を仕様化し、計画書を memory/agents/implementation-planner/<task>.md に書く
   - コード変更はしない

2. spec-test-author  [任意 — 仕様 first フロー時のみ]
   - 計画書を読み、tests/pcb_assembly/ 配下にテスト（生きた仕様書）を書く
   - 本番コード（src/pcb_assembly/）は一切触らない
   - 期待される失敗と仕様根拠を memory/agents/spec-test-author/<task>.md に記録
   - plan-implementer と並列実行可能（後述「並列化の判断基準」パターン A）

3. plan-implementer
   - 計画書（必要なら spec-test-author のノートも）を読み、実装＋型＋lint をグリーン化
   - spec-test-author が engagement されていればテストは編集しない（実装だけで通す）
   - 計画外の判断は memory/agents/plan-implementer/<task>.md に記録

4. code-reviewer
   - diff と計画書を突き合わせ、verdict（approve / request-changes）を
     memory/agents/code-reviewer/<task>.md に出す
   - orchestrator が裁定：must-fix → 3 に差し戻し／should-fix（構造改善）→ 5 へ／
     誤検出 → 却下（理由を orchestrator のノートに記録）

5. code-simplifier
   - 公開IFを保持しつつ内部を簡素化（reviewer の should-fix も対応対象）
   - 簡素化内容を memory/agents/code-simplifier/<task>.md に記録
   - 大きく書き換えた場合は 4 で再レビュー

6. docs-keeper
   - README/docstring を最小保守
   - 整備内容を memory/agents/docs-keeper/<task>.md に記録
```

3〜5 は code-reviewer が approve を出すまで繰り返す。approve 後に 6 へ進む。

## spec-test-author を挟むかの判断

| 状況                                                          | 推奨                                     |
| ------------------------------------------------------------- | ---------------------------------------- |
| 公開 API の仕様が明確で、振る舞いの契約を先に固めたい         | **挟む**                                 |
| ハードウェア絡みで実機テスト・skip 条件の設計を先行で詰めたい | **挟む**                                 |
| 仕様が探索的で、実装しながら詰める必要がある                  | 挟まない（plan-implementer が一括）      |
| trivial な変更・1 関数追加                                    | 挟まない                                 |
| 既存仕様への bug fix で、再現テスト → 修正の流れにしたい      | **挟む**（再現テストを spec として固定） |

挟まない場合は従来通り `plan-implementer` がテストと実装を同時に書く。

## 並列化の判断基準

### パターン A：spec-test-author × plan-implementer の並列

`tests/pcb_assembly/` と `src/pcb_assembly/` は disjoint なので、両 agent は **同時起動できる**。仕様 first フローでもシリアル実行に縛られない。

```
implementation-planner
        ↓
   ┌────┴────┐
   ↓         ↓
spec-test-author  plan-implementer   ← 並列起動（同じ計画書を入力に）
   ↓         ↓
   └────┬────┘
        ↓
   合流チェック：make test-no-hardware を流して、テストが実装に対して期待通り通る／落ちるか確認
        ↓
   code-reviewer →（approve まで反復）→ code-simplifier → docs-keeper
```

**並列化の前提条件**:

- planner の「公開インターフェース案」が **シグネチャレベル** で確定していること（関数名・引数・戻り値型）
- ハードウェアリソース（実カメラ／実 Klipper）の同時アクセスがないこと（`@mark_hardware` テストは実行せず、実機確認はユーザーに委ねる）
- `tests/helpers.py` の更新は spec-test-author 側に寄せる（plan-implementer は触らない）

**合流時の処理**:

- まず `make format && make type` を通す
- `make test-no-hardware` を流し、spec-test-author の意図通りに pass/fail しているか確認
- 不整合（例：implementer が IF を変えた、spec-test-author が観点を取りこぼした）があれば、該当 agent を再呼び出し
- 全 pass になったら code-reviewer へ

**並列化を避けるケース（このパターン）**:

- 公開 IF がシグネチャレベルまで決まっていない（探索的実装）
- planner の計画が抽象的で、両 agent が違う解釈を取りそう
- ハードウェアテストが支配的で、両者が同一機材を必要とする

### パターン B：モジュール分割による並列

- **独立したモジュール・機能** ごとに implementer / spec-test-author を分割
- **インターフェース境界が明確** で、互いの実装詳細に依存しないとき
- 例：`geometry` 配下を 1 セット、`control` 配下を別セットで並行
- パターン A と組み合わせて、各モジュール内でさらに spec-test-author × plan-implementer の並列も可
- レビューは実装完了後にモジュール単位で並列起動できる（code-reviewer を複数同時に）

### 並列化を避けるケース（共通）

- インターフェース未確定で並行実装すると衝突するとき
- データフロー上で順序依存があるとき
- `tests/helpers.py` を複数 spec-test-author が同時編集するとき（fake Impl の追加は逐次推奨）
- 同一ハードウェアリソースに同時アクセスが必要なとき

### 並列実行の運用

- 並列 implementer は `memory/agents/plan-implementer/<task>-<instance>.md` 形式でファイル分離
- 並列 spec-test-author も `memory/agents/spec-test-author/<task>-<instance>.md` で同様に分離
- 他の implementer / spec-test-author に影響する IF 変更は同ファイルに「IF変更通知」セクションで明記
- 並列 simplifier / reviewer も同様にファイル分離
- 並列発火は 1 メッセージにまとめる（skill `maximize-parallels` 参照）

## チェックポイント

orchestrator が各段階で確認する。

| タイミング              | 確認事項                                                                                                    |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| planner 完了時          | 計画書に「公開IF案／実装ステップ／テスト観点／リスク」が揃っているか                                        |
| spec-test-author 完了時 | テストが `tests/pcb_assembly/` 配下のみに書かれているか、3rd-party モックが無いか、仕様根拠の対応表があるか |
| implementer 完了時      | `make format && make type && make test-no-hardware` がグリーン。spec-test-author 引継ぎ時はテスト未編集     |
| reviewer 完了時         | verdict が明記され、全 must-fix に根拠（仕様箇所・再現手順）が添えてあるか                                  |
| simplifier 完了時       | 公開IF不変・全テスト通過・簡素化ノートに変更点列挙                                                          |
| 並列完了時              | **全並列分マージ後に1回 `make format && make type && make test-no-hardware` を必ず通す**                    |
| docs-keeper 完了時      | README/docstring が現状コードと整合                                                                         |

実機テスト（`make test` の `@mark_hardware` 分）はどの agent も実行しない。検証は `make test-no-hardware` までとし、実機確認はユーザーに委ねる（`memory/MEMORY.md` の「No hardware test execution」）。

## Failure mode と対処

| 状況                                                | 対処                                                             |
| --------------------------------------------------- | ---------------------------------------------------------------- |
| planner の計画が曖昧                                | ユーザーに追加質問（spec-test-author / implementer に進めない）  |
| spec-test-author が仕様の欠落に直面                 | planner を再呼出し、計画書を更新してから再開                     |
| spec-test-author 完了後、テストが落ちる（想定済み） | plan-implementer に引き継いで実装で通す                          |
| implementer が「テストが間違っている」と判断        | テストは編集せず spec-test-author に差し戻し、仕様根拠を再確認   |
| implementer がテスト失敗（spec-test-author 不在時） | 再実装、または計画書の見直し（planner 再呼び出し）               |
| reviewer の must-fix に implementer が異議          | orchestrator が計画書を根拠に裁定。仕様が曖昧なら planner 再呼出 |
| reviewer ⇄ implementer の往復が 2 回を超える        | orchestrator が停止し、ユーザーに判断を仰ぐ                      |
| simplifier が「これ以上簡素化できない」と判断       | 次の docs-keeper へ進む                                          |
| 並列 implementer で IF 衝突発覚                     | 並列を中断し、planner で IF を再合意                             |

## 起動コマンド例

ユーザー指示「エージェントチームで HeightPlane の調整機能を追加して」に対し、orchestrator（メイン）が実行する流れ：

1. 要件を確認し、ブランチを作成する
2. implementation-planner agent を起動して計画策定
3. 計画を確認・承認（必要ならユーザーに確認）
4. （仕様 first なら）**spec-test-author と plan-implementer を 1 メッセージで並列起動**（パターン A）
    - 計画書が抽象的なら spec-test-author を先行、IF 確定後に plan-implementer を起動
5. 合流時に `make format && make type && make test-no-hardware` で整合確認
6. code-reviewer agent を起動し、verdict を裁定（must-fix → implementer 差し戻し）
7. approve 後に code-simplifier agent を起動。大改修なら 6 で再レビュー
8. 最後に docs-keeper agent を起動し、最終検証 → コミット

各 agent 起動時は `subagent_type` で指定し、必要なコンテキスト（計画書パス、対象ファイル、前段ノートのパス等）を prompt に含める。並列発火時は 1 メッセージにまとめる（skill `maximize-parallels`）。
