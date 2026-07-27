---
name: agent-team-startup
description: orchestrator（メインエージェント）が implementation-planner →（任意 spec-test-author）→ plan-implementer → code-reviewer ⇄ code-simplifier を統括する標準フロー、モデル構成（全 agent inherit + effort で差別化）、委譲と並列化の判断基準。ユーザーが「エージェントチームで進めて」と言ったり、複数モジュールにまたがる中〜大規模変更を要求したときに参照する。
---

# エージェントチーム起動手順

ユーザーから「エージェントチームで」と指示があった場合、または中〜大規模な変更（複数モジュールにまたがる実装・リファクタリング）を行う場合の進行手順。

詳細な並列化パターン・Failure mode・起動例は [reference.md](reference.md) に分離してある。判断に必要になった時点で読む。

## 前提

- `settings.json` に `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` が設定済み（必須）
- メインエージェントは `orchestrator`（`settings.json` の `"agent": "orchestrator"`。`claude --agent orchestrator` でも起動可）
- 各 agent は `.claude/agents/` 配下に定義済み（orchestrator, implementation-planner, spec-test-author, plan-implementer, code-reviewer, code-simplifier）
- 中間メモは `memory/agents/<agent-name>/<task-slug>.md` に書く（詳細は `memory/agents/README.md`）

## 役割とモデル / effort 構成

全 agent がセッションのモデルを継承する（`model: inherit`）。速度・コスト・深さの差別化は **`effort` 一本**で行う。呼び出し時に Agent tool の `model` / `effort` パラメータで一時的に上書きもできる（frontmatter より優先）。

| agent                  | effort       | 役割                                                                     |
| ---------------------- | ------------ | ------------------------------------------------------------------------ |
| orchestrator           | セッション値 | メインエージェント。統括・委譲・レビュー裁定・合流検証・ユーザーとの対話 |
| code-reviewer          | xhigh        | レビュー（仕様準拠・バグ・規約・テスト品質）。コードは修正しない         |
| implementation-planner | high         | 要件の仕様化・計画書作成（コードは書かない）                             |
| plan-implementer       | high         | 計画に基づく実装・テスト・グリーン化                                     |
| spec-test-author       | medium       | 仕様テスト（`tests/` のみ、`src/` は触らない）                           |
| code-simplifier        | medium       | 公開 IF 維持の簡素化 + docstring / README 同期（レビューはしない）       |

orchestrator に `effort` を設定していないのは、frontmatter の effort がセッション値を上書きし、ユーザーの `/effort` が効かなくなるため。セッション既定は `settings.json` の `effortLevel` で与える。

**effort の再調整**: 上表は出発点であって固定値ではない。実タスクで観察し、品質が保てるなら下げる。Opus 5 は `low` / `medium` でも品質が落ちにくく、これがコストとレイテンシの主要な制御になる。

## 標準サイクル

```
0. orchestrator（メイン）
   - 要件確認・ブランチ作成・以降の統括。src/ tests/ は自分で編集しない
   - ユーザーへの質問はここだけが行える（サブエージェントは AskUserQuestion を持たない）

1. implementation-planner  [中〜大規模時。小〜中規模は orchestrator が自分で計画]
   - 要件を仕様化し、計画書を memory/agents/implementation-planner/<task>.md に書く
   - 確認事項があれば計画書に列挙し、orchestrator が中継する

2. spec-test-author  [任意 — 仕様 first フロー時のみ]
   - 計画書を読み、tests/pcbasm/ 配下にテスト（生きた仕様書）を書く
   - 本番コード（src/pcbasm/）は触らない
   - 期待される失敗と仕様根拠を memory/agents/spec-test-author/<task>.md に記録
   - plan-implementer と並列実行可能（reference.md パターン A）

3. plan-implementer
   - 計画書（必要なら spec-test-author のノートも）を読み、実装＋型＋lint をグリーン化
   - spec-test-author が engagement されていればテストは編集しない（実装だけで通す）
   - 計画外の判断は memory/agents/plan-implementer/<task>.md に記録

4. code-reviewer
   - diff と計画書を突き合わせ、verdict（approve / request-changes）を
     memory/agents/code-reviewer/<task>.md に出す
   - 見つけた指摘は確信度を添えて全件報告する（ふるいにかけない）
   - orchestrator が裁定：must-fix → 3 に差し戻し／should-fix → 5 へ／
     誤検出 → 却下（理由を orchestrator のノートに記録）

5. code-simplifier
   - 公開 IF を保持しつつ内部を簡素化（reviewer の should-fix も対応対象）
   - 変更で古くなった docstring / README を同期する
   - 内容を memory/agents/code-simplifier/<task>.md に記録
   - 大きく書き換えた場合は 4 で再レビュー
```

3〜5 は code-reviewer が approve を出すまで繰り返す。approve 後に orchestrator が最終検証してコミットする。

## 委譲するかどうか

チームを起動する前に、その仕事が委譲に見合うかを判断する。サブエージェントは文脈を再構築し、探索し直し、報告を返し、orchestrator がそれを読み直す。この往復を上回る利得が要る。

- **チームを起動する** — 複数ファイル・複数モジュールにまたがる実装や調査。独立して並列に進められる作業がある
- **単独委譲で足りる** — 対象が明確な 1 モジュールの実装（`plan-implementer` 単独）
- **委譲しない** — 数回のツール呼び出しで orchestrator 自身が終えられる仕事。自分の作業の検証・ダブルチェック（検証は orchestrator のループ内で行う）

1 体で足りる仕事を分割して複数体に投げない。spawn 数は低く保つ。

## spec-test-author を挟むかの判断

| 状況                                                          | 推奨                                     |
| ------------------------------------------------------------- | ---------------------------------------- |
| 公開 API の仕様が明確で、振る舞いの契約を先に固めたい         | **挟む**                                 |
| ハードウェア絡みで実機テスト・skip 条件の設計を先行で詰めたい | **挟む**                                 |
| 既存仕様への bug fix で、再現テスト → 修正の流れにしたい      | **挟む**（再現テストを spec として固定） |
| 仕様が探索的で、実装しながら詰める必要がある                  | 挟まない（plan-implementer が一括）      |
| trivial な変更・1 関数追加                                    | 挟まない                                 |

挟まない場合は `plan-implementer` がテストと実装を同時に書く。

## 並列化の基本条件

`tests/pcbasm/` と `src/pcbasm/` は disjoint なので、`spec-test-author` と `plan-implementer` は同時起動できる。前提条件:

- planner の「公開インターフェース案」が**シグネチャレベル**（関数名・引数・戻り値型）で確定していること
- ハードウェアリソース（実カメラ／実 Klipper）の同時アクセスがないこと
- `tests/helpers.py` の更新は spec-test-author 側に寄せること

モジュール分割による並列、並列時のファイル分離規約、避けるべきケースは [reference.md](reference.md) を参照。並列発火は 1 メッセージにまとめる（skill `maximize-parallels`）。

## チェックポイント

orchestrator が各段階で確認する。

| タイミング              | 確認事項                                                                                                |
| ----------------------- | ------------------------------------------------------------------------------------------------------- |
| planner 完了時          | 計画書に「公開 IF 案／実装ステップ／テスト観点／リスク」が揃っているか。確認事項があれば中継したか      |
| spec-test-author 完了時 | テストが `tests/pcbasm/` 配下のみに書かれているか、3rd-party モックが無いか、仕様根拠の対応表があるか   |
| implementer 完了時      | `make format && make type && make test-no-hardware` がグリーン。spec-test-author 引継ぎ時はテスト未編集 |
| reviewer 完了時         | verdict が明記され、全 must-fix に根拠と確信度が添えてあるか                                            |
| simplifier 完了時       | 公開 IF 不変・全テスト通過・ドキュメントが現状コードと整合                                              |
| 並列完了時              | 全並列分マージ後に 1 回 `make format && make type && make test-no-hardware` を通す                      |

実機テスト（`make test` の `@mark_hardware` 分）はどの agent も実行しない。実機が物理的に動作するため。検証は `make test-no-hardware` までとし、実機確認はユーザーに委ねる（`memory/MEMORY.md` の「No hardware test execution」、`settings.json` でも deny 済み）。

## 参照

- 並列化パターン・Failure mode・起動例：[reference.md](reference.md)
- tool 呼び出しレベルの並列化：skill `maximize-parallels`
- 各 agent の詳細：`.claude/agents/<name>.md`
