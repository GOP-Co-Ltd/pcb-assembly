# CLAUDE.md

Claude Code がこのリポジトリで作業する際に常時参照するガイダンス。
詳細な手続きは `memory/` と `.claude/skills/` にオフロードしている。

## 応答言語

ユーザーへの応答は日本語で行う。

- 散文・説明・要約・確認・質問・計画は日本語で書く
- コード・コマンド・ファイルパス・識別子・ログ/エラーの引用など、原文を保つべきものは原語のまま残す
- コミットメッセージ・PR/MR タイトル等は「Git 運用」規約（英語形式）に従い、日本語化しない
- 技術用語は無理に訳さず、自然な範囲でカタカナ/英語を併用してよい

## 開発原則

LLM コーディングで陥りがちなミスを減らすための行動指針。慎重さは**成果物の正しさ**に向ける — 確認の往復を増やす方向や、検証を重ねる方向には向けない。trivial なタスクでは判断で柔軟に運用してよい。

### 1. 実装前に考える

**仮定を隠さない。ただし、聞かなくても決まることは決める。**

- 軽微な選択（命名、既存パターンに倣えば決まるもの、同等案のどちらか）は自分で決め、採用理由を 1 行添える
- 解釈の違いで成果物が実質的に変わる点だけを確認する。スコープの変更と破壊的操作は必ず事前に確認する
- 仮定を置いたなら明示する。黙って前提を作らない
- もっと単純な方法があるなら言う。正当な理由があれば押し返す

### 2. シンプルさを優先

**問題を解く最小限のコード。投機的な実装はしない。**

- 頼まれていない機能は足さない
- 単発の用途しかないコードに抽象化は入れない
- 要求されていない「柔軟性」「設定可能性」は持ち込まない
- 起こり得ないシナリオに対するエラーハンドリングは書かない。境界（ユーザー入力・外部 API・ハードウェア）でのみ検証する

自問する: 「これをシニアエンジニアが見たら過剰だと言うか?」Yes なら単純化する。

### 3. 外科的な変更

**触る必要があるものだけ触る。依頼された範囲を、依頼された粒度で仕上げる。**

- 周辺コード・コメント・整形を「ついでに改善」しない
- 壊れていないものをリファクタしない
- 自分なら違う書き方をするとしても既存スタイルに合わせる
- 無関係な dead code に気付いたら指摘する。勝手に消さない
- 自分の変更で生じた orphan (未使用 import / 変数 / 関数) は消す。元から dead だったコードは頼まれない限り消さない
- 依頼が誤っている、もっと良い方法がある、と判断したら 1 文で述べたうえで依頼どおり進める。黙って狭めたり広げたり作り替えたりしない
- タスクは最後までやり切る。完了と報告するのは実際に終わったときだけ。終えられなかった部分があれば、残りを進めたうえで何が欠けているかを明記する

判定基準: diff の全行が、ユーザーの要求から直接トレースできるか?

### 4. ゴール駆動の実行

**成功条件を定義する。検証できるまでループする。**

タスクを検証可能なゴールに変換する:

- 「バリデーションを足す」→「不正な入力に対するテストを書いて通す」
- 「バグを直す」→「再現するテストを書いて通す」
- 「X をリファクタする」→「変更前後でテストが通ることを確認する」

複数ステップのタスクでは短い計画を先に提示する。

### 5. 簡潔に伝える

**結論から書く。読み手が次に何をするかを変えない記述は落とす。**

- 応答は要点に絞る。前置きと注意書きは短くし、本題に紙幅を使う
- 作業を終えたら最初の 1 文で結果を述べる（何が起きたか／何が分かったか）。詳細と根拠はその後に置く
- 短さより読みやすさを優先する。削るのは「含める情報」であって、文を断片・略語・矢印の連なりに圧縮することではない
- ディスク上の成果物（レポート、Markdown、docstring）も同じ。埋めるための節・重複する要約・定型文を足さない
- 説明を求められたら概要で答える。詳細な解説は明示的に求められたときだけ書く

## プロジェクト概要

PCB アセンブリ装置の制御コード。Raspberry Pi 5 + Klipper + KiCAD を前提とし、ハードウェア抽象化レイヤ（HAL）、ビジョン処理、制御ロジック、3D 幾何計算を Python 3.12+ で実装する。

主要モジュール構成（`src/pcbasm/`）:

- `hal/` — ハードウェア抽象化（カメラ、Klipper、プローブ、サーボ等）
- `vision/` — 画像処理・キャリブレーション・特徴検出
- `posctrl/` — Board/オフセットの位置合わせ共通制御（setup, tour, board/position/offset 調整）
- `pasting/` — ペースト塗布専用ロジック（applicator, calibration, fill_path, probe, height 等）
- `pnp/` — Pick and Place 用の名前空間（将来用）
- `geometry/` — 3D 座標と幾何計算（Transform, HeightPlane, 軌跡生成等）
- `pcb/` — PCB 設計情報の抽象化（KiCAD 読込、配置管理）
- `visualization/` — 塗布パス・高さ面・PCB のレンダリング/可視化

このほか `src/web/` にブラウザ操作 UI を 2 プロセスで置く（どちらも FastAPI）。`src/web/api/` が機体ごとの backend WebAPI（port 8081）、`src/web/ui/` が LAN に 1 つ立てる UI frontend（port 8080。ページ描画と `/m/{machine_id}/api/**` の backend 中継）。開発・運用の操作は WebUI のジョブとして提供する。リポジトリ直下の `scripts/` には `migrate_codex.py` のみを置く。

## 開発コマンド

- `make setup` — 開発環境のセットアップ
- `make test-no-hardware` — ハードウェア以外のテスト実行。**Claude が使う検証コマンドはこれ**
- `make test` — 全テスト実行（ハードウェア含む・e2e は除く）。実機が動くため Claude は実行しない（`settings.json` で deny 済み）。実機確認はユーザーが行う
- `make test-e2e` — WebUI フルスタック E2E（uvicorn 実起動・HTTP/WS/MJPEG）
- `make format` — pre-commit フック（ruff, docformatter 等）
- `make type` — pyright 型チェック
- `make run` — `format` → `test` → `type` を順実行（実機テストを含む。ユーザー用）
- `make api` / `make api-dev` — WebUI backend サーバー起動（port 8081、dev は auto-reload）
- `make api-fake` — fake カメラで backend 起動（隔離 data_dir/port、手動/ブラウザ E2E 用）
- `make ui` / `make ui-dev` — UI frontend サーバー起動（port 8080、dev は auto-reload）
- `make ui-fake` — `api-fake`（8099）を上流にした frontend 起動（隔離ポート 8098、手動/ブラウザ E2E 用）

## 不変の原則

### カプセル化

- クラスの内部実装の詳細・属性は基本的に private（`_` prefix）
- `__init__` で設定される属性は原則 private
- 外部から参照する必要がある属性のみ public

### テスト方針（要点）

- 公開インターフェースと振る舞いをテストする（内部実装はテストしない）
- 実データ・実オブジェクト優先。fake してよいのは自前 HAL ABC のみ、3rd-party 表面のモックは禁止
- テストは `class TestXxx` 形式に集約
- ハードウェアテストは `@mark_hardware` で分離
- 詳細：skill `testing-strategy`（正典）、`hardware-test`、`refactor-conventions`

### WebUI 設計（ロジックは pcbasm、JS は薄いラッパー）

計算・ドメインロジックは pcbasm（`src/pcbasm/`）に集約し、算出結果はエンドポイントで公開する。`src/web/api/` の router、`src/web/ui/` の page ハンドラ、JS はいずれも薄いラッパー（入出力変換・DOM 操作・表示更新）に徹する。

- **Do**: 解決済み値・派生値・集計はサーバが算出して返す。JS は API レスポンスをそのまま表示に流し、編集後はサーバ応答（または再取得）で更新する。クライアント検証は UX 最小限（空欄・数値パース可否）に留める
- **Do**: frontend（`src/web/ui/`）の page ハンドラは backend の JSON（pydantic モデル）を受けてテンプレートに渡すだけ。装置の状態を持たず、backend が返した値を再計算しない
- **Do**: 表示文字列（マシンの label など）の組み立てはサーバ側で行う。クライアントで組むと表示規則が JS に散る
- **Don't**: ドメインルール（正値・整数・enum 許容値・階層 override 解決・幾何計算など）を JS や router に複製しない。ローカル状態を楽観的に再計算してサーバと二重管理しない。サーバが既に返す値を再導出しない
- **判定基準**: 「この結果はサーバの真実と一致すべきか?」Yes なら Python へ。「描画のための座標/色変換か?」Yes なら JS 可（SVG 座標変換・色補間・極座標は移さない）
- 詳細：skill `webui-thin-wrapper`、実起動検証は `webui-e2e`

## Git 運用

### ブランチ

- `main` から `<種別>/<日付>/<内容>` で分岐（例：`refactor/20260430/height-plane`）
- 種別：`feature`, `fix`, `refactor`, `docs`, `chore`
- `main` に直接 commit しない。`main` への merge はユーザー判断

### コミットメッセージ

- `<種別>(<スコープ>): <内容>`
- 種別：`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`
- 1 コミット 1 関心事

## 自走開発フロー

要件確認 → ブランチ作成 → 実装 → `make format && make type && make test-no-hardware` → コミット

- 検証通過前のコミットはしない
- 要件外の機能は実装しない
- 計画外の判断はコミットメッセージに理由を残す

## エージェントチーム

ユーザーから「エージェントチームで進めて」と指示があった場合、または中〜大規模変更時は skill `agent-team-startup` を参照。逆に「委譲せず自分で」「シーケンシャルに」と指示された場合、および 1〜数モジュールに収まる変更を通しで仕上げる場合は skill `solo-dev-cycle`（同じ工程を単独でロール切替しながら回す版）を参照。

メインエージェントは `orchestrator`（`.claude/settings.json` の `"agent"` キーで設定。統括・委譲・レビュー裁定・ユーザーとの対話を担い、`src/` `tests/` は自分で編集しない）。標準サイクル：`implementation-planner` →（任意 `spec-test-author`）→ `plan-implementer` → `code-reviewer` ⇄ `code-simplifier`。各 agent の中間メモは `memory/agents/<agent-name>/<task>.md`。

全 agent が `model: inherit`（セッションのモデルを継承）で、速度・コスト・深さは **`effort` で差別化**する：`code-reviewer` は xhigh、`implementation-planner` と `plan-implementer` は high、`spec-test-author` と `code-simplifier` は medium。orchestrator はセッション既定（`settings.json` の `effortLevel`）に従う。

- `spec-test-author` は `tests/` 専用（`src/` は触らない）。仕様 first フローで `plan-implementer` と **並列実行可能**（公開 IF がシグネチャレベルで確定していることが前提）
- `code-simplifier` は仕上げ役。簡素化に加え、変更に伴う docstring / README の同期も担う
- **委譲は spawn 数を低く保つ**。数回のツール呼び出しで済む仕事、および自分の作業の検証は委譲しない（検証はメインループで行う）
- **ユーザーに質問できるのは orchestrator だけ**。サブエージェントは `AskUserQuestion` を持たないため、質問は報告に含めて返し orchestrator が中継する

## コンテキスト管理 (compact)

長いセッションの context 圧縮 (compact) で判断構造が失われる事故を防ぐ仕組みを `.claude/` に組み込んである。詳細は [compact-prep skill](.claude/skills/compact-prep/SKILL.md)。

- **60% 通知。** statusLine (`.claude/scripts/statusline.sh`) が context 使用率を毎ターン算出し、閾値 (既定 60%) を超えると警告 marker を書く。`UserPromptSubmit` hook がそれを検出し、区切りで `/compact-prep` → `/compact` を促す。自動 compact に先を越されないための先回り。閾値 60% は 1M context 前提の設定 (60% でもまだ ~400k の作業余地が残る)。
- **`/compact-prep`。** `/compact` 直前に実行する skill。要約に残りにくい判断構造 (採用/却下した案・現在フェーズ・委譲したサブエージェント) を `${TMPDIR:-/tmp}/claude-compact-state/<session_id>.md` へ退避する。
- **圧縮後の復旧。** `PostCompact` hook が圧縮を marker で記録し、次の `UserPromptSubmit` hook が state file・TaskList・本ファイルの決定事項を読み戻すよう指示する。圧縮サマリーの next step は仮説として扱う。
- 依存: hook / statusLine は `python3` のみを使う（かつて `jq` に依存していたが、未導入環境では fail-open で無音になり通知も復旧も起きなかったため置き換えた）。配線は `.claude/settings.json` の `statusLine` / `hooks`。marker は `${TMPDIR:-/tmp}` 配下で session_id ごとに分離するため、並行タスクと衝突しない。

## 参照先マップ

### `memory/` — ユーザーとの対話で確立された規約・好み

- `memory/MEMORY.md` — インデックス
- `memory/feedback_*.md` — 個別フィードバック（規約・好み）
- `memory/agents/` — エージェント間の中間メモ

### `.claude/skills/` — 手続きのオフロード先

- `testing-strategy` — テスト方針の正典（4 区分・検証対象優先順位・書く/書かないリスト・モック方針）
- `hardware-test` — ハードウェアテストの記述・実行手順
- `refactor-conventions` — カプセル化・エラーハンドリング・リファクタ技法の詳細規約
- `webui-e2e` — WebUI を実サーバーで E2E 検証する手順（make test-e2e / api-fake + ui-fake、常駐サーバー kill の回避策）
- `webui-thin-wrapper` — WebUI を薄いラッパーに保つ手順（ロジックの pcbasm 集約・JS/router からのロジック除去・許容範囲の線引き）
- `agent-team-startup` — エージェントチームの起動・委譲判断・並列化手順
- `solo-dev-cycle` — 委譲せず単独で 計画 → テスト → 実装 → リファクタ → ドキュメント の 5 段階をロール切替で回す手順
- `maximize-parallels` — 並列 tool 呼び出しの判定基準と典型パターン
- `edit-dot-claude` — `.claude/` 配下の編集を /tmp 経由で行い permission prompt を抑える手順
- `do-on-worktree` — 進行中の別タスクを止めず、main 分岐の worktree で裏作業を進める手順
- `gitlab-mr` — ブランチを GitLab に push し glab で MR を作成する手順（対象ブランチはデフォルト main）
- `merge-main` — MR を出す前に最新の main を取り込み、コンフリクトを解消する手順
- `japanese` — ユーザーへの応答を日本語に切り替える（上記「応答言語」を一時的に明示する用途）
- `compact-prep` — `/compact` 直前に作業状態を state file へ退避し、圧縮後 hook が読み戻す（statusLine が 60% で `/compact-prep` の実行を促す）
