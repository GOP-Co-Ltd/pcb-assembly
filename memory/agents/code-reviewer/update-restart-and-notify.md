# 更新まわり 3 件（restart / -15 / 通知）レビュー

## verdict: request-changes

must-fix 2 件（M1: `-15` 修正の範囲が広すぎる / M2: `restart_services()` が flock を取らない）。
それ以外は仕様準拠・規約準拠とも良好で、検証は 3 本とも pass。

## must-fix

### M1. `returncode < 0` は「exec できなかった」も成功にする

- 対象: `src/web/selfupdate/service.py:136-143`
- 問題: `run_command` は Popen が `OSError` を出したとき
    `CommandResult(argv, -1, "<bin> を起動できません: ...", timed_out=False)` を返す
    （`src/web/selfupdate/steps.py:161-164`）。新ガードがこれを拾って `None`（成功）にする。
    main は `not result.ok` で「終了コード -1」の理由を返していた
- 実測（このブランチで実行）:

    ```
    run_command -> returncode= -1 timed_out= False ok= False
    output= /tmp/.../nonexistent-sudo を起動できません: [Errno 2] No such file or directory
    schedule_restart -> None
    ```

- 併せて `-1` は `-SIGHUP` と同値。sentinel と実シグナル死が原理的に区別できない
- 緩和要因: `sudo_bin` 不在は `restart_permitted` が先に捕まえる（`sudo -n -l` も同じ
    OSError 経路で `ok=False` になるため）。残る到達経路は preflight 通過後の
    exec/fork 失敗（EAGAIN / ENOMEM）と `systemctl_bin` 側の不整合
- 影響: サイレント失敗。report は RESTARTING / SUCCEEDED のままで再起動されず、誰も気付けない
- 直す範囲: launch 失敗をシグナル死と区別できる形にする（`run_command` 側で
    `timed_out` と同格の明示フラグを持たせる、あるいは `returncode < -1` に絞る）。
    回帰テスト「`sudo_bin` が存在しないときは理由を返す」を追加する
- 確信度: 高（実行で確認）／深刻度: 中〜高

### M2. `restart_services()` が flock を取らず、同居機の相方の更新を殺せる

- 対象: `src/web/selfupdate/runner.py:154-182`
- 問題: `self.running` は**このプロセスの**更新スレッドしか見ない。`state_dir` の既定は
    api / ui とも `PROJECT_ROOT/data/selfupdate`（`selfupdate/settings.py:39`,
    `web/api/settings.py:105`, `web/ui/settings.py:61`）なので `update.lock` は 2 プロセス共有。
    `web/ui/update_api.py:19` 自身が「単一実行ロック（flock。同居機の backend とも共有する）」と明記
- 再現手順: 同居機で UI frontend が `/update` から更新中（flock 保持・`uv sync` 実行中）
    → 機体ページで「ファームウェア再起動」を押す → backend 側は `running=False` で通過
    → `sudo systemctl restart --no-block pcbasm-api.service pcbasm-ui.service`
    → systemd が `pcbasm-ui.service` を止め、`uv sync` が中断
- 根拠: 本ブランチ自身のテスト `tests/web/selfupdate/test_runner.py:501`
    `test_running_update_is_not_interrupted` が「`uv sync` の最中に unit を落とすと
    中途半端な依存で起動不能になる」と書いている事象そのもの。守れているのは同一プロセスだけ
- 副次: `self.running` チェックが `self._guard` の外なので、同一プロセス内でも `start()` と TOCTOU
- 直す範囲: `restart_services()` でも非ブロッキング flock 取得を試し、取れなければ理由を返す
- 確信度: 高（機序）／中（同時操作が要る）／深刻度: 高（復旧に ssh）

## should-fix

- **S1. `schedule_restart` の戻り値を捨てている** — `runner.py:176-181` の
    `threading.Thread(target=schedule_restart, ...)` が `str | None` を握り潰す。
    `schedule_restart` の docstring は「拒否されたら呼び出し元へ返して report に残せる」と
    書いているのに、この経路では report にも log にも残らない。preflight 通過後の失敗
    （unit 名変更で Unit not found 等）が完全に不可視。`src/web` には logger の前例あり
    （`web/ui/proxy.py:37`）。確信度: 高／深刻度: 中
- **S2. 「ファームウェア再起動」に確認ダイアログが無いまま爆発半径が広がった** —
    `templates/base.html:39` + `static/js/app.js:179-184` は即 POST。main までは Klipper MCU
    だけだったが、本変更で LAN 全員の WebUI が落ちる。更新側は `confirm()` を安全弁 4 として
    明記している（`web/ui/update_api.py:21`）。`data-requires-control` の操作権ゲートはある。
    ユーザー要求どおりの挙動なので仕様違反ではなく、誤クリック耐性の判断をユーザーに戻したい。
    確信度: 高（事実）／中（対応要否）
- **S3. `FirmwareRestartResponse` の `restart_units` と `ok` に消費者が無い** —
    `restart_units` は JS もテストも読まない（app.js は `message` / `warning` だけ）。
    `ok` は `_klipper_action` が失敗時に 502 を送出するので常に True の定数。
    AGENTS.md 原則 2。確信度: 高／深刻度: 低
- **S4. `/api/update-notice` が全ページ・全タブから 60 秒ごとに git を約 8 プロセス起こす** —
    `_self_source` → `plan()` → `capture_state`（`selfupdate/repo.py:123-166` で rev-parse ×2 /
    symbolic-ref / rev-list / status / ls-files / log 等）。backend 中継でもう一式。frontend は無認可。
    既存 `/api/self-update` にも同じ性質はあるが、そちらは更新ページを開いている間だけ。
    確信度: 中／深刻度: 低〜中

## nit

- **N1. `restart_notice()` の表示文字列に余分な空白が入った（既存機能の退行）** —
    `unit_summary` 抽出で「（…）を再起動します。」→「（…） を再起動します。」。実測:
    `'更新後に backend WebAPI・UI frontend（pcbasm-api.service pcbasm-ui.service） を再起動します。…'`。
    3 件の要求からトレースできない変更で、文字列を pin するテストが無いので誰も落ちない。確信度: 高
- **N2. blocker 付きの更新は通知に出ない** — `update_available` は `blocker is None` を要求する
    （`selfupdate/report.py:197-205`）。dirty / ローカル commit で止まっている機体はバッジが出ない。
    「押せば進むか」としては正しいが、要求 3 の読み方次第では取りこぼし。確信度: 中
- **N3. hostname による重複畳み込みは `PCBASM_HOSTNAME` 次第で外れる** — backend は
    `resolve_machine_id`、frontend は `socket.gethostname()`。同居機で別名を設定すると
    1 件が「2 件」に。テストは未設定ケースだけを固定。確信度: 中
- **N4. `restart_units()` のキャッシュを firmware-restart が引き継ぐ** — 起動順に依存を付けて
    いないので backend が先に上がると ui が inactive と観測され、その値がプロセス生存中ずっと
    使われる。既存挙動だが頻度の高い経路に載った。確信度: 低〜中
- **N5. バッジは prefix ガードを迂回する形になっている** — `href` を持たないので
    `test_every_navigation_link_is_machine_prefixed` の走査に入らない。設計としては正しいが、
    DOM 上の実 href を確かめるテストは無い

## 仕様準拠

- 要求 1: 満たす。ただし backend ホストの active unit 限定なので、frontend が別ホストの構成では
    frontend は再起動されない（合意した `active_units()` の範囲どおり）。成功経路は実 Moonraker が
    要るため自動テスト無し → ユーザーの実機確認が要る
- 要求 2: 原因分析は妥当。修正の範囲だけが広すぎる（M1）
- 要求 3: 満たす（N2 の但し書きあり）
- 要求外の混入: N1 のみ

## 規約

- カプセル化（`_` prefix）、`str | None` 返却、表示文字列のサーバ組み立て、JS の薄さ: 準拠
- testing-strategy: 3rd-party をモックせず実 git + 実スタブ実行ファイル。`test_service.py` の
    `kill -TERM $$` スタブは -15 回帰をちゃんと落とせる形。ただし M1 の launch 失敗ケースは未カバー
- 成果物汚染（`</content>` 等）: 無し

## 検証結果

- make format: pass
- make type: pass（pyright 0 errors）
- make test-no-hardware: pass（3140 passed, 122 deselected, 124s）
- 実機テストは未実行（規約どおりユーザーが実施）
