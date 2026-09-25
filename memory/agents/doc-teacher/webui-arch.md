# docs/architecture.md・docs/webui.md 理解度テスト

## 対象

- ファイル: `docs/architecture.md`、`docs/webui.md`
- 読者と用途: 開発者が、パッケージ構成と backend（8081）/ frontend（8080）の分担を理解し、新しい機能・ページ・API をどこにどう足すかを判断する

## 評価用問題

### Q1 backend に新しい endpoint `GET /api/foo` を足した。ブラウザから `/m/{machine_id}/api/foo` で使えるようにするため、frontend（`src/web/ui/`）側で何を変更するか。

- 要点: 何も変更しない／`proxy.py` が `/m/{machine_id}/api/**` を backend の `/api/**` へそのまま中継するため

### Q2 新しい endpoint が機体の設定を書き換える。この endpoint に何を付けるか。操作権を持たない人が呼ぶと何が返るか。

- 要点: `ControlDep` を付ける／423 が返る

### Q3 新しいページの JS から backend の `/api/foo` を呼ぶ。JS に `fetch("/api/foo")` と直接書いてよいか。よくないなら代わりにどう書くか。

- 要点: 書かない／`app.js` の `api()` か `fetchApi()` に `/api/foo` を渡す（理由: 機体の prefix が付かない、またはテストで禁止、のどちらかに触れていれば可）

### Q4 「はんだ塗布」（pasting）タブに新しいジョブを足す。ジョブ関数と登録はどこに書くか。

- 要点: `src/web/api/jobs/pasting/` に新しいモジュールと `register` を作る／`pasting/__init__.py` の `register_pasting_jobs` から呼ぶ

### Q5 新しいジョブの `JobDefinition.name` を `measure_foo`、そのページの feature slug を `foo_measure` にしたい。よいか。

- 要点: よくない／`name` と feature slug は同じ文字列にする

### Q6 posctrl タブに新しいジョブを登録した（`hidden` ではない）が、`src/web/ui/` には何も足していない。このままだと何が起きるか。直すには何をするか。

- 要点: テストが落ちる／`layout.py` の `FEATURE_GROUPS` と `FEATURE_TEMPLATES` に feature を足す

### Q7 ジョブではない新しい表示ページを dev タブに足す。表示名はどこに書くか。テンプレートを `JOB_TEMPLATES` に入れるべきか。

- 要点: `layout.py` の `FEATURE_LABELS`／入れない（入れると 503 になる）

### Q8 新しいページに、backend の応答に含まれていない集計値を表示したい。frontend の `pages.py` で計算してテンプレートへ渡してよいか。よくないならどうするか。

- 要点: よくない／backend の応答モデルにその値を足し、`pages.py`（`_FEATURE_CONTEXT` / `_JOB_FEATURE_CONTEXT`）で backend の応答をテンプレートへ渡す

### Q9 塗布経路の新しい計算が必要になった。計算を新しい router の中に書いてよいか。どこに置くか。

- 要点: 書かない／`src/pcbasm/`（塗布なら `src/pcbasm/pasting/`）に置き、router からは呼ぶだけ

### Q10 新しいジョブを足すとき、ジョブの開始・中止のための router も書く必要があるか。

- 要点: 要らない／`routers/jobs.py` の共通 API が扱う

### Q11 新しいジョブは PCB ファイルを生成するだけで装置を動かさない。`JobDefinition` で何を設定するか。その設定によって、ジョブ終了時の通知音はどうなるか。

- 要点: `uses_machine=False`／成功音・失敗音は鳴らない

### Q12 frontend 専用ホストで、frontend が読む `machines.toml` の場所を変えたい。どの環境変数を使うか。`PCBASM_CONFIG_DIR` では変えられるか。

- 要点: `PCBASM_UI_MACHINES_FILE`／`PCBASM_CONFIG_DIR` では変わらない

## 保留問題

### H1 新しく作ったページを `/m/kurousagi/dev/foo` で開くと 404 になった。まずどのファイルの何を確認するか。

- 要点: `src/web/ui/layout.py`／`FEATURE_GROUPS`（dev タブ）に `foo` が登録されているか

### H2 `data/webui/` というディレクトリ名がわかりにくいので、名前だけを変えたい。変えてよいか。

- 要点: 変えない／運用データが孤立するため（構造を変えるなら既存データの読込・移行を扱う）

### H3 posctrl タブに足したジョブのページに、専用画面は要らない。`FEATURE_TEMPLATES` に何を指定するか。`FEATURE_LABELS` に表示名を足す必要はあるか。

- 要点: `posctrl/job.html`（または全タブ共用の `job.html`）／要らない（backend の `JobDefinition.label` を使う）

## ラウンド記録

### R0（執筆）

- architecture.md: 79 → 127 行（+61%）。増加理由: 読者の用途（API・ジョブ・ページをどこに足すか）に対応する節が無かった（欠落）。「機能を足すときの変更先」節を追加
- webui.md: 229 → 230 行。architecture.md の新節への案内を 1 行追加。利用者向けの本文は変えていない
- 事実確認: `routers/*` の prefix、`app.py` の `include_router`、`dependencies.py` の `ControlDep`（423）、`proxy.py` の mount、`app.js` の `withBase`／`fetch` 集約テスト（`tests/web/ui/test_layout.py`）、`catalog.py`、`jobs/pasting/__init__.py`、`layout.py`、`pages.py` の `feature_page`／`_job_spec`（503）、`tests/web/ui/test_pages.py::TestTabsCatalogConsistency`

### R1（architecture.md 127 → 124 行、webui.md 230 行）

| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q7 | 正解 | - | - |
| Q8 | 不正解 | 誤読 | なし。要点 2（`pages.py` の `_FEATURE_CONTEXT` / `_JOB_FEATURE_CONTEXT` で backend の応答を渡す）が欠落。文書の同じ段落に明記されているので R2 で同じ問題を再出題する |
| Q9〜Q12 | 正解 | - | - |

- 仕上げの肥大化見直し（オーケストレーター指示）: 既存記述との重複を 3 か所削った。「リクエストとジョブ」の「API の中継は proxy.py」と「JobContext でログ・進捗・プロンプト」の 2 文は新節に具体的に書いてあるので削除した。新節の冒頭 2 文は 1 文にまとめた。「frontend で値を計算しない」は既存の再計算禁止の段落と重なるので、応答モデルに足す文へ統合した
- Q3 の理由（prefix が付かない／テストで禁止）は要点の必須項目ではないので正解とした

### R2（architecture.md 124 → 126 行）

| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q7 | 正解 | - | - |
| Q8 | 不正解 | 曖昧（誤読 2 回目のため昇格） | 「pages.py で渡す」と「応答モデルに足す」の 2 文の関係が読み取れず、生徒は後の文だけを採っていた。2 段の番号付き手順（1. backend の応答モデルに足す → 2. `_FEATURE_CONTEXT` / `_JOB_FEATURE_CONTEXT` で渡す）にした |
| Q9〜Q12 | 正解 | - | - |

### R3（architecture.md 126 行、変更なし）

| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| Q1〜Q12 | 正解 | - | - |

- Q8: 両要点（応答モデルに足す／`pages.py` でテンプレートへ渡す）を含む。根拠に挙げた「ページを足す」節に両方が書かれているので正解とした
- 評価用が全問正解したので、保留問題 H1〜H3 を出題する

### 保留問題（汎化確認）

| 問 | 判定 | 原因 | 直したこと |
| -- | ---- | ---- | ---------- |
| H1〜H3 | 正解 | - | - |

- 最終行数: architecture.md 126 行（執筆前 79）、webui.md 230 行（執筆前 229）

### mdformat 対応（126 → 127 行）

- 原因: 「ジョブを足す」で番号付きリスト 1〜2 の後に表を挟み、`3.` から再開していた。mdformat-mkdocs が再開番号を振り直し、HTML が変わって失敗した
- 対処: タブ別の表をリストの前に出し、手順を 1〜3 の連続したリストにした。内容は変えていない。表の列幅は mdformat が揃えた
- 教訓: リストの途中に表やブロックを挟んで番号を再開させない
