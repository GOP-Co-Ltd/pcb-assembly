# WebUI からのソフトウェア更新（webui-self-update）レビュー

計画書: `/home/gop/.claude/plans/claude-webui-api-ui-git-ssh-git-pull-web-robust-rocket.md`
前段: `memory/agents/spec-test-author/webui-self-update.md` / `memory/agents/plan-implementer/webui-self-update.md`
対象: `git diff main`（作業ツリー）+ 未追跡ファイル。main = `d74f96f`

## 2 巡目 verdict: approve

1 巡目の must-fix 3 件（M1 smoke / M2 sudo listing / M3 merge timeout）はいずれも
**機構まで含めて直っている**ことを実測で確認した。should-fix 10 件も本質を捉えた対応。
残りは should-fix 5 件（うち 3 件は今回の修正で新たに入ったもの）と nit で、
マージを阻害するものは無い。**実機投入前に R1（1 行）と R3（1 行）は入れることを勧める。**

---

## 1 巡目 must-fix の検証結果

### M1 import smoke が frontend 専用機で必ず失敗する → 直った（確信度 高）
- `steps.py:28-51` に `SMOKE_MODULES`（unit → モジュール）と `smoke_modules(units)`。
  `runner.py:293-306` が active unit から導出し、0 台なら skip して「省略しました」を記録
- **本番経路に既定引数が残っていない**ことを確認: `smoke_command` の呼び出しは
  `runner.py:296` の 1 箇所だけで、必ず `modules` を明示している
  （`grep -rn "smoke_command" src/` で確認）
- 4 形状すべてが `tests/web/test_update_host_shapes.py` で固定されている
  （ui 単体 / api 単体 / 同居 / 0 台）。専用機ケースは「smoke の argv に `web.api.app`
  が現れないこと」まで見ており、指摘の本質（装置依存を読ませない）を捉えている

### M2 `sudo -n -l` の折り返し → 直った（確信度 高）
- `service.py:78-91`: `COLUMNS=1000` を渡して sudo 自身に折り返させず、加えて
  `_unwrapped()` で継続文字と改行を畳んでから照合する二重化
- 実 sudo の出力形（`\` + 8 桁字下げ）を模した文字列で `_unwrapped` を実行して確認:
  api 単体・同居機の 2 変種が一致し、**許可されていない ui 単体は不一致のまま**
  （fail-closed が緩んでいない）
- `COLUMNS` が効く根拠: sudo は tty が無いときだけ `COLUMNS`/`LINES` を見る
  （`sudo_get_ttysize` の env フォールバック）。今回はまさに tty 無しの経路
- `tests/web/test_update_host_shapes.py::TestWrappedSudoListing` が 80 桁折り返しの
  スタブで両方向（通る / 落ちる）を固定

### M3 merge のタイムアウトと killpg → 直った（確信度 高）
- `settings.py:55-58` に `merge_timeout=600.0` を新設し、`git_timeout=30.0` は
  「ローカル完結の読み取り専用」とコメントで限定。`repo.py:224-231` が merge に適用
- タイムアウト時のメッセージが「作業ツリーが中途半端な状態になっている可能性があります
  （ssh して `git status` と `git lfs pull` を確認）」と**壊れうる事実を隠していない**。
  README にも「ここで打ち切ると作業ツリーが中途半端に残る」と明記
- 600 秒の妥当性: この repo の LFS 実体は 11 ファイル・`.git` 13MB で、smudge が
  10 分を超えるのは回線障害時くらい。障害時は timeout より先に ssh の
  ConnectTimeout / TCP 側で落ちる公算が高い。**妥当**と判断する

## M2 の裁定（COLUMNS+正規化 vs 終了コード）について

**裁定は妥当。** 実行時判定を `sudo -n -l <argv>` に変えると、呼び出しログに
`restart` を含む行が残り `conftest.py:267` の `restart_calls()`（"restart を含む行"）が
preflight を再起動と誤検出する。これは spec-test-author の保護対象ファイルを
書き換えないと直らず、得られる robustness の差（実測で正規化は実 sudo の形に耐える）に
見合わない。

ただし**食い違いは 1 箇所だけ実在する**（下記 R1）。設置時の自己検査が
`api` と `ui` の**単体 2 変種しか**確かめておらず、**同居機で実際に使う
3 変種目（api + ui）を検査していない**。実行時はその 3 変種目を文字列照合で見るので、
「install は OK、preflight で落ちる」が原理的に起こりうる組み合わせが残っている。

---

## 1 巡目 should-fix の検証結果

| # | 指摘 | 対応 | 判定 |
| --- | --- | --- | --- |
| S1 | 更新スレッドの例外が report に残らない | `_run` に `except Exception` → `report.fail()` + 保存、`active_units` は `except OSError` → `()` | ✔ 本質を捉えている。`GET status` が 200 を保つことも新テストで固定 |
| S2 | units 空でも sudoers 必須 | `restart_permitted` が units 空で即 `None`。`SUCCEEDED` が到達可能に | ✔ 新テストで「sudoers 未設置の開発機でも成功し、`sudo` を 1 度も呼ばない」まで固定 |
| S3 | マシン切替が 404 | `current_suffix="dev/update"` + 相互リンク | ✔ |
| S4 | api/ui の state_dir 既定が別導出 | backend の `update_dir` をリポジトリ直下固定に（`data_dir` から切り離し） | ✔ 判断も正しい（ロックが守る対象は worktree であって data_dir ではない）。ただし README が追随していない（R3） |
| S5 | 再起動失敗を握り潰す | `schedule_restart` が `str | None` を返し `report.fail()` へ | ✔ 新テストあり |
| S6 | unit 差分チェック未実装 | `stale_units()` / `stale_unit_warning()` → `report.warnings` → 画面 | ✔ 実機で実測: この Pi の `pcbasm-api.service` を正しく stale と判定し、差分は本 MR が足した 2 行だけ（**非特権・0.01 秒・誤検出なし**）。要求どおり自動 install はしない |
| S7 | `git status` の失敗を検査せず | 失敗を `(None, 理由)` に | ✔ |
| S8 | 無認可 fetch の占有 | `check_fetch_timeout=20.0` | ✔ 面積は残るが 6 分の 1 に |
| S9 | テストの穴 | `tests/web/test_update_host_shapes.py` 15 件、`closing_client` を `@contextmanager` 化 | ✔ 条件付き assert も新ファイル側で確定的に固定された |
| S10 | JS が表示文字列を組む | `head_label` / `failed_detail` をサーバ側で | ✔ |

nit も `tail(text,0)`（空文字を返す）、`uv sync` / smoke への `git_env()`、
README の「丸ごと置き換える」明記、`/update` ⇄ `/m/<id>/dev/update` 相互リンク、
`_dirty_path` の quotepath（「表示専用」と docstring に明記）まで対応済み。

---

## 残る should-fix（今回の修正で入ったものを含む）

### R1. 設置時の自己検査が同居機 argv を検査していない（新規）
- 対象: `scripts/install-update-sudoers.sh:84-94`（`verify_sudoers`）
- 問題: `for unit in "${API_UNIT}" "${UI_UNIT}"` の 2 変種しか `sudo -n -l <argv>` に
  かけていない。実行時に同居機が使うのは 3 変種目
  `... --no-block pcbasm-api.service pcbasm-ui.service`
- 壊れ方: 同居機で `install` が「許可を確認しました」と言ったのに、実行時の
  preflight が 3 変種目で落ちる、が原理的に起こりうる（`render_sudoers` は 1 つの
  ヒアドキュメントから 3 行を出すので確率は低いが、**食い違いを潰す目的の検査が
  肝心の形を見ていない**）。ループに 1 要素足すだけ
- 確信度: 高（カバレッジの欠落は事実）

### R2. 警告機能（`stale_units`）の例外が更新全体を FAILED にして再起動を止めうる（新規）
- 対象: `src/web/selfupdate/runner.py:309`、`service.py:153-157`
- 問題: `stale_units` は `run_command(("bash", "-c", ...))` を **try/except なしで**
  step 6（report 確定）と step 7（再起動）の**手前**で呼ぶ。`bash` が PATH に無い等で
  `FileNotFoundError` が上がると `_run` の `except Exception` が拾って
  `report.fail()` → **sync も smoke も通ったのに再起動されない**
- 壊れ方: 「注意書きを出すためだけの処理」が本流（再起動）を殺す。発火確率は低い
  （systemd unit の PATH には必ず bash がある）が、順序を入れ替えるか
  try/except で `()` に倒すだけで消える
- 確信度: 機構は高／発火条件は低

### R3. README の `PCBASM_API_UPDATE_STATE_DIR` 既定が S4 の変更と食い違う（新規）
- 対象: `README.md`（backend の env 一覧）「既定 `<data_dir>/selfupdate`」
- 問題: S4 で `Settings.update_dir` は `data_dir` から切り離され、既定は
  **リポジトリ直下の `data/selfupdate`** になった。`PCBASM_API_DATA_DIR` を設定した
  機体で README を信じると、単一実行ロックの位置を誤解する（S4 が直した当の論点）
- 確信度: 高

### R4. `update.js` が新フィールドを無防備に読む（版ずれ耐性が models 側だけ）（新規）
- 対象: `src/web/ui/static/js/update.js:53,58`（`for (const w of run.warnings)` /
  `run.warnings.length`）
- 問題: pydantic 側は既定値で「新 frontend × 旧 backend」に耐えるようにしてあるのに、
  JS 側は `warnings` が無い応答で `TypeError` を投げる。`render()` は `pollOnce` の
  `tick` から `try` の外で呼ばれるので、**ポーリングが無言で止まる**
- 壊れ方: 本 MR の範囲では起きない（このフィールドを持たない backend は
  `/api/update/status` 自体を持たず 404 で綺麗に失敗する）が、次にフィールドを足す MR で
  「1 台だけ更新した状態」に踏む。`run.warnings ?? []` の 1 箇所
- 確信度: 機構は高／本 MR での発火は無し

### R5. `stale_units` の `bash -c` 文字列と、seam を通らない `bash`（新規）
- 対象: `src/web/selfupdate/service.py:153-157`
- 問題: `run_command(("bash", "-c", f'source "{script}"; render_unit "{target}"'))` は
  実質シェル実行で、argv を f-string で組んでいる（本 MR の他の全経路が避けている形）。
  値はサーバ側固定（`repo_root` と `UNIT_NAMES` のキー）なので**注入は成立しない**が、
  (a) `bash` だけ `UpdateSettings` の絶対パス seam を通っていない、
  (b) `source` するのは **git pull 直後の新しい `web-service.sh`** である、
  という 2 点はコメントに残す価値がある（(b) は smoke / uv sync と同じ信頼境界なので
  新しいリスクではない。実際 `web-service.sh` の top-level は変数定義と関数定義だけで、
  `BASH_SOURCE` ガードにより dispatch もしない）
- 確信度: 中（規約解釈）

---

## nit

- `tests/web/update_support.py:160-173` の `_wrapped()` に実 sudo の継続文字 `\` が無い。
  実装の `_unwrapped` は `\` 付きでも通ることを別途確認したが、スタブが実物より
  1 段甘いままなのはこの修正の趣旨（実物に寄せる）と噛み合わない
- `steps.py:55` `smoke_command(..., modules=ALL_SMOKE_MODULES)` の既定は本番から
  1 度も使われない（テスト専用）。将来 `smoke_command(settings)` と書かれると M1 が
  静かに戻る。`tests/web/test_update_host_shapes.py:99` が既定を固定しているので
  「意図的な既定」ではあるが、必須引数にして契約テスト側を直す方が安全
- `make api-fake` / `ui-fake` は `PCBASM_API_DATA_DIR` を隔離するが update state は
  隔離されない（リポジトリ直下固定）。ロック共有は正しい挙動なので**トレードオフとして
  妥当**だが、手動 fake セッションが実機の最終更新記録を上書きする
- `tests/web/test_update_host_shapes.py` は `tests/web/` 直下にある唯一のテストで
  ミラーレイアウト外（対応する `src/web/test_update_host_shapes.py` は無い）。
  `tests/web/selfupdate/` が保護対象なので実務的な妥協ではある
- 同ファイル 148-150 行に 3 連続の空行
- docformatter が識別子を改行で割った docstring が残る
  （`tests/test_package.py:52` の「torch」、`tests/test_web_service_script.py:272` の
  `` `start-\nlimit-hit` ``。`tests/web/selfupdate/test_steps.py:47` は保護対象）
- `git` が無いホストでは `GET /api/update/status` が 500 になる
  （`active_units` は `except OSError` で守ったが `capture_state` → `_git` は素通し）
- `systemctl is-active` が `activating` / `failed` の unit を active と数えないため、
  クラッシュループ中の unit は更新後に再起動されない（`restart_notice` は
  「再起動しません」と正しく言う）
- `_execute` は 0 unit のホストでも PREFLIGHT を「OK」として記録する（何も検査していない）

## 未対応 2 件の判定（落として良いか）

- **`wait()` が public → 落として良い。** 規約が禁じているのは「private 属性をテスト
  都合で public にする」ことで、`wait()` は内部状態を露出しない。バックグラウンド実行を
  持つオブジェクトの待機メソッドとして自然で、docstring に用途も書いてある
- **`update_support.py` と conftest の重複 → 落として良いが、負債として記録する。**
  今回むしろ増えた（`write_sudo_stub` / `write_systemctl_stub` が両方に存在）。
  ただし両者は役割が分かれた: conftest 側は spec-test-author が固定した契約テスト用の
  最小スタブ、`update_support` 側は実 sudo に寄せた忠実版。統合するには保護対象ファイルを
  触る必要がある。**次に `tests/web/selfupdate/` へテストを足す人が弱い方のスタブを
  掴む**ので、conftest 側に「忠実版は tests/web/update_support.py」と 1 行書ければ十分

---

## 検証結果（2 巡目・自分で実行したもの）

- `uv run pytest tests/web/selfupdate tests/web/test_update_host_shapes.py
  tests/web/api/routers/test_update.py tests/web/ui/test_update.py
  tests/test_update_sudoers_script.py tests/test_web_service_script.py tests/test_package.py
  tests/web/ui/test_layout.py tests/web/ui/test_control_ui.py -m "not hardware"`
  → **267 passed**
- `uv run pyright src/web/selfupdate src/web/api/routers/update.py src/web/ui/update_api.py
  tests/web/test_update_host_shapes.py tests/web/update_support.py` → **0 errors**
- `git diff -- tests/web/selfupdate tests/test_update_sudoers_script.py` が空
  = **spec-test-author の契約テストは 1 文字も変わっていない**
- `_unwrapped()` を実 sudo 形（`\` 継続 + 8 桁字下げ）に対して実行 → 許可 2 変種は一致、
  未許可の 1 変種は不一致（fail-closed 維持）
- `stale_units()` をこのホストの実 unit に対して実行 → 正しく stale、差分は本 MR の 2 行のみ
- テスト実行後も `data/selfupdate/` は生えていない（S4 の変更で隔離が壊れていない）
- `make format` / 全体テスト / e2e は orchestrator の再実行結果を採用（3207 / 110 passed）
- 実 sudo・実 systemd は制約により未実行。M2 の最終確認は実機での
  `./scripts/install-update-sudoers.sh install` 時の出力で足りる
