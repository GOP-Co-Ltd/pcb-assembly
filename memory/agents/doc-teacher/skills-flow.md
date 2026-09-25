# 作業フロー skill と agent 定義 理解度テスト

## 対象
- ファイル:
    - `.claude/skills/{agent-team-startup/SKILL.md, agent-team-startup/reference.md, solo-dev-cycle, compact-prep, do-on-worktree, github-pr, merge-main, maximize-parallels, edit-dot-claude, japanese}/SKILL.md`
    - `.claude/agents/{code-reviewer, code-simplifier, implementation-planner, orchestrator, plan-implementer, spec-test-author}.md`
    - `.agents/skills/migrate-claude/SKILL.md`（Codex 専用。生徒には読ませない）
    - 同名の `.agents/skills/<name>/SKILL.md` に同じ改稿を反映（Codex 向けの表記差は保つ）
- 読者と用途: コーディングエージェントが、作業フロー（単独/チーム開発、worktree、PR、main 取り込み、並列化、compact 準備）と各 agent の役割・境界を誤りなく実行する。
- 生徒に読ませるのは `.claude/` 側だけ（約 1220 行）

## 評価用問題
### Q1 ユーザーが「3 つのモジュールにまたがる機能を追加して」と依頼した。チームや並列の指示はない。どのフロー（skill）で進めるか。理由も。
- 要点: solo-dev-cycle で単独に進める
- 要点: チーム（agent-team-startup）はユーザーが「エージェントチームで」「並列で」と明示したときだけ。規模は判断材料にならない

### Q2 solo-dev-cycle の段階 2（テスト実装）を終えて段階 3 へ進んでよいかを、何を実行して何を確かめて判断するか。
- 要点: 新規テストだけを `uv run pytest -m "not hardware" <path>` で実行する（`-m "not hardware"` 必須）
- 要点: 期待どおりに失敗する（red）ことを確かめる

### Q3 solo-dev-cycle で、振る舞いを変えない純粋なリファクタリングを行う。省略してよい段階と、省略してはいけない段階を答えよ。
- 要点: 段階 2（テスト実装）は省略してよい（既存テストの green が担保）
- 要点: 段階 1・3 と段階 4 の自己レビューは省略しない

### Q4 チーム運用中、code-reviewer の報告は must-fix 0 件・should-fix 2 件だった。orchestrator はこの後何をするか。順に答えよ。
- 要点: approve として扱う（should-fix は残ってよい）
- 要点: code-simplifier に should-fix の対応と docstring / README 同期を委譲する
- 要点: 大きく書き換えたら code-reviewer で再レビュー、その後 orchestrator が最終検証してコミット

### Q5 spec-test-author がテストを書いた後、plan-implementer が「このテストは間違っている」と判断した。plan-implementer はテストを直してよいか。どうするか。
- 要点: テストは編集しない
- 要点: spec-test-author に差し戻し、仕様根拠を再確認させる

### Q6 spec-test-author が `src/web/api/routers/` の変更に対するテストを書く。テストをどこに置くか。実装側にバグを見つけたら `src/` を直してよいか。
- 要点: `tests/web/api/routers/`（`src/` を `tests/` にミラー）
- 要点: `src/` は直さない。ノートに記録して plan-implementer に引き継ぐ

### Q7 チーム運用中、変更は `src/pcbasm/geometry/` の 1 モジュールに収まる。計画を implementation-planner に委譲するか。
- 要点: 委譲しない。orchestrator が自分で計画する
- 要点: 委譲するのは複数モジュールにまたがるとき

### Q8 PR を出そうとしたら、`git log HEAD..origin/main --oneline` に commit が並んだ。PR 作成までに行う操作を順に答えよ。
- 要点: rebase ではなく `git merge origin/main` で作業ブランチに取り込む
- 要点: conflict があれば解消する
- 要点: `make format && make type && make test-no-hardware` で検証してから push し、github-pr の手順で PR を作る

### Q9 main の取り込みで多数の file が conflict した。`git checkout --theirs .` でまとめて解消してよいか。どうするか。
- 要点: してはいけない（一括上書き禁止）
- 要点: 両者の意図を保持して file ごとに解消し、判断が割れる conflict は何が衝突しているか名指しでユーザーに確認する

### Q10 do-on-worktree で worktree の作業と PR 作成を終えた。(a) ユーザーから何も言われていない今、`ExitWorktree` を呼ぶか。(b) その後ユーザーが「元のタスクに戻って」と言った。`ExitWorktree` の action に何を指定するか。`remove` を使ってよいか。
- 要点: (a) 呼ばない。ユーザーが「戻って」等と言ったときに呼ぶ
- 要点: (b) `action: "keep"`。`remove` は使わない（未マージの作業を消さないため）
- R2 で問題文を変更（R1 の「呼ぶならどの action か」は、呼ばないと答えた生徒に action を答える動機がなく問題不良）

### Q11 `.claude/agents/foo.md` に 4 回に分けて Edit を入れたい。どう作業するか。
- 要点: `/tmp/dot-claude-work/<name>` に cp して、そこで Edit する
- 要点: 最後に cp で `.claude/agents/foo.md` へ書き戻す

### Q12 同じ file に対する Read と Edit を 1 メッセージで並列に出してよいか。理由も。
- 要点: 出してはいけない（逐次）
- 要点: Edit は事前の Read を要求するため、並列だと Edit が失敗する

### Q13 compact-prep 実行中、`bash .claude/scripts/get-session-id.sh` の出力が空（exit 1）だった。どうするか。
- 要点: 推測した名前で state file を作らない
- 要点: 「session_id を取得できないため準備未完了」と報告して停止する

### Q14 implementation-planner がユーザーに確かめたい点を 5 つ見つけた。どう扱うか。
- 要点: ユーザーに直接聞けない（AskUserQuestion がない）。報告に「確認事項」として返し orchestrator が中継する
- 要点: 軽微なものは自分で決め、確認事項は成果物が変わる点だけ 2〜3 個に絞る
- 要点: 暫定案を添えて計画書は書き切る（確認待ちで止まらない）

## 保留問題
### H1 code-reviewer が、根拠を示しきれないが気になる指摘を見つけた。報告に含めるか。
- 要点: 含める。「確信度：低」と明記する
- 要点: 省いてよいのは好みの範囲（nit）だけ

### H2 `.claude/settings.json` に permission を足したい。edit-dot-claude の /tmp 経由の手順を使うか。
- 要点: 使わない。skill `update-config` で扱う

### H3 spec-test-author が `@pytest.mark.api_contract` を初めて使う。何に注意するか。
- 要点: 未登録なので `pyproject.toml` の `markers` に登録する
- 要点: `--strict-markers` のため未登録だとテストが失敗する

## ラウンド記録
### R0（執筆。1291 行 → 1289 行、.claude 側 + migrate-claude 合計）
事実確認で直したこと:
- solo-dev-cycle の使い分け表が「複数モジュールの大規模変更 → agent-team-startup」と書き、CLAUDE.md / agent-team-startup の「チームは明示時だけ」と矛盾 → 明示の有無で分ける表に置換
- solo-dev-cycle 段階 2 の `pytest <path>` に `-m "not hardware"` が無い（CLAUDE.md 違反）→ `uv run pytest -m "not hardware" <path>`
- spec-test-author / plan-implementer / agent-team-startup が担当範囲を `tests/pcbasm/` `src/pcbasm/` に限定し、`tests/web/` の存在と矛盾（description は `tests/`）→ `tests/` `src/` に統一
- spec-test-author: `api_contract` マーカーは pyproject.toml に未登録（`--strict-markers`）→ 登録が要ると明記
- agent-team-startup: 「3〜5 を approve まで繰り返す」と reference の「approve 後に simplifier」が食い違い → 3⇄4 を must-fix が消えるまで、approve 後に 5
- planner 委譲基準「中〜大 / 小〜中」が重複 → 複数モジュールか 1 モジュールか（agent-team-startup, reference, orchestrator）
- reference.md のパターン A 前提条件は SKILL.md と重複 → 参照に置換
- merge-main: AGENTS.md に無い「自走開発フロー」節を参照 → 「Git 運用」。「ローカル main を作業ブランチに直接 commit」の誤記修正。rebase 理由の重複を圧縮。Claude 版の実機テスト行を「実行しない」に
- maximize-parallels: `make test -m hardware` は存在しない形 → `pytest -m hardware`。agent 並列を「常に並列」と書きチーム明示条件と矛盾 → 明示時だけ。AGENTS.md「Custom Agents」への誤参照を削除
- compact-prep: Claude が実行しない `make run` を検証基準にしていた → `make format && make type && make test-no-hardware`
- github-pr: main が進んでいるときに merge-main を挟む導線が無い → 事前チェックに 1 行
- do-on-worktree: cwd 持ち越しで他 worktree を切り替えた事故（ユーザー memory）→ `git -C` を 1 行
- edit-dot-claude: `rm -rf` を例示してから禁止する混乱した注記 → `rm -r` のみ
- Codex 側: merge-main（同じ 4 点、実機行は Codex 版のまま）、github-pr（merge-main 導線）、agent-team-startup（tests/ src/）

### R1（1289 行 → 1289 行。agent-team-startup/SKILL.md は 126 → 127 行で、全体では ±0 に丸まる）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1 | 正解 | - | - |
| Q2 | 正解 | - | - |
| Q3 | 正解 | - | - |
| Q4 | 不正解（再レビュー条件と docstring 同期が欠落） | 埋没 | サイクル後の要約文が step 5 の「大きく書き換えたら 4 で再レビュー」を落としていた。要約文に再レビューを入れた |
| Q5 | 正解 | - | - |
| Q6 | 正解 | - | - |
| Q7 | 正解 | - | - |
| Q8 | 正解 | - | - |
| Q9 | 不正解（判断が割れる conflict をユーザーに名指しで確認する、が欠落） | 誤読 | なし。merge-main 手順 4 に明記済み。R2 で再出題 |
| Q10 | 不正解（action keep / remove 禁止が欠落） | 問題不良 | 問題文を (a)(b) に分けた |
| Q11 | 正解 | - | - |
| Q12 | 正解 | - | - |
| Q13 | 正解 | - | - |
| Q14 | 不正解（直接聞けず orchestrator が中継する、が欠落） | 誤読 | なし。「質問の扱い」の冒頭文に明記済み。R2 で再出題 |

### R2（1289 行 → 1289 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q3, Q5〜Q13 | 正解 | - | Q3 の「段階 5 も条件付きで省略可」は文書どおりなので正解扱い |
| Q4 | 不正解（再レビュー条件と docstring 同期が欠落。根拠に orchestrator.md を引用） | 矛盾 | orchestrator.md の進め方 7 に再レビューが無く、agent-team-startup と食い違っていた。7 に should-fix と「大きく書き換えたら 6 に戻って再レビュー」を追記 |
| Q14 | 不正解（直接聞けず orchestrator が中継する、が再び欠落） | 埋没（誤読 2 回目） | 中継の記述が箇条書き前の導入文にだけあった。「質問の扱い」を番号付き手順にし、4 番目に「報告に列挙して返す。中継は orchestrator」を置いた |

### R3（1289 行 → 1290 行）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q8, Q10〜Q14 | 正解 | - | Q4 は approve の語は無いが、差し戻さずに simplifier・再レビュー条件・最終検証へ進む行動が揃っているので正解。Q14 は「報告に列挙して返す」で中継の要点を満たす |
| Q9 | 不正解（判断が割れる conflict をユーザーに名指しで確認する、が欠落。R1 に続き 2 回目） | 埋没 | merge-main 手順 4 の箇条書き 4 項目に埋もれていた。file ごとの番号付き手順に組み替え、2 番目を「解消せずに止めてユーザーに確認」にした（.agents 側も同文） |

### R4（1290 行 → 1290 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q5, Q7〜Q14 | 正解 | - | Q7 は根拠に「委譲するかどうか」を挙げたが、同節の往復コストの原則が答えを支えるので正解。Q9 は「名指し」の語が無いが、判断が割れたらユーザーに確認する行動は満たすので正解 |
| Q6 | 不正解（「ノートに記録して plan-implementer に引き継ぐ」が欠落） | 誤読 | なし。spec-test-author.md「役割」3 行目に明記され、R1〜R3 は正解。1 ラウンドのぶれとして R5 で同じ問題を出す |

### R5（最終。1290 行 → 1290 行、改稿なし）
| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q3, Q5〜Q14 | 正解 | - | Q5 の「テストを直させる」は、spec-test-author が再確認して誤りなら直す流れと両立するので正解 |
| Q4 | 不正解（simplifier への docstring / README 同期の依頼が欠落） | 誤読 | なし。orchestrator.md 進め方 7 と agent-team-startup 標準サイクル 5 に明記され、R3・R4 は正解。1 ラウンドのぶれ |

ループ終了。保留問題 H1〜H3 をメインの汎化確認に渡した。

振り返り: 効いた直しは実物との矛盾（tests/pcbasm 限定、チーム起動条件、orchestrator の再レビュー欠落）と、導入文や箇条書きに埋もれた要点の番号付き手順化（planner の質問の扱い、merge-main の conflict 解消）。行数は 1291 → 1290 で増えていない。

### 保留問題（汎化確認）
| 問 | 判定 | 備考 |
| -- | ---- | ---- |
| H1 | 正解 | 出題文は「含めるか、どう書くか」で、要点 2（省いてよいのは nit だけ）は問うていないため要点 1 で判定 |
| H2 | 正解 | - |
| H3 | 正解 | R0 で追記した api_contract 未登録の記述が効いた |

保留 3/3 正解。改稿なしで終了。
