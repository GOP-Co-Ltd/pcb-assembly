---
name: solo-dev-cycle
description: sub-agentへ委譲せず、計画、テスト、実装、自己review、簡素化、文書同期を逐次進める開発フロー。ユーザーが委譲しないよう明示したとき、またはagent-team-startupを使うほどではない小〜中規模の変更を通しで仕上げるときに使う。
---

# Sub-agent を使わず開発サイクルを回す

メイン agent が同じ context の中で役割を切り替え、1 タスクを最後まで仕上げる。
trivial な変更はこの Skill を使わず直接対応してよい。

## 使い分け

- ユーザーが「委譲せず」「一人で」「sequential に」と明示した場合は規模を問わず使う
- 1〜数 module に収まる小〜中規模変更では、委譲コストを避けるため使う
- 独立した大きな作業が複数あり、ユーザーが agent 利用を明示した場合は
    `agent-team-startup` を使う

## サイクル

1. **計画**: 公開 IF、実装手順、テスト観点、リスクを確定する。
2. **仕様テスト**: 振る舞いを変える場合は `tests/` に先にテストを書く。
3. **実装**: 計画の範囲内で最小限の production code を実装し、テストを通す。
4. **自己 review と簡素化**: diff と計画を突き合わせ、公開 IF を保って整理する。
5. **文書同期**: 今回の変更で古くなった docstring、README、Skill、memory だけを直す。

各段階に入るときはユーザーへ短く現在地を伝える。別段階の気付きは記録し、担当段階で
処理する。

## 段階ゲート

- 計画: 公開 IF と検証可能な成功条件が確定している
- 仕様テスト: 新規テストが仕様どおりに失敗する。振る舞い不変の変更では省略可
- 実装: `make format`、`make type`、`make test-no-hardware` が通る
- 自己 review: `git diff` の全行が要求へトレースでき、不要な変更がない
- 文書同期: コードと関連文書が整合する。対象がなければ変更しない

実機テストはどの段階でも実行せず、ユーザーに委ねる。pytest を直接実行する場合は
必ず `-m "not hardware"` を付ける。

## 自己 review

記憶ではなく `git diff` と計画を根拠に確認する。

- 仕様と公開 IF に一致するか
- 境界値、None、例外、単位、座標系に抜けがないか
- private 実装を直接テストしていないか
- 3rd-party 表面をモックしていないか
- 要求外の抽象化、整形、dead code 削除が混ざっていないか

判断が割れる場合は勝手にスコープを変えず、ユーザーへ確認する。

## 記録

長いタスクで判断ログが必要な場合は、`memory/agents/orchestrator/<task-slug>.md` の
1 ファイルに計画、計画外判断、review 指摘と対応を段階ごとに追記する。

## 参照

- チームへ委譲する場合: [agent-team-startup](../agent-team-startup/SKILL.md)
- テスト方針: [testing-strategy](../testing-strategy/SKILL.md)
- 実装・リファクタ規約: [refactor-conventions](../refactor-conventions/SKILL.md)
