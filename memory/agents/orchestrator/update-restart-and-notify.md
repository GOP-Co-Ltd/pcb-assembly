# ソフトウェア更新まわりの 3 件（restart / -15 / 通知）

## 要求

1. 「ファームウェア再起動」ボタンで pcbasm の web サービス（api / ui）も再起動する
2. 「更新して再起動」で出る「再起動コマンドが失敗しました（終了コード -15）」の原因特定と修正
3. 更新が存在するときに通知を出す

ユーザー確認済みの分岐:

- 通知対象は **UI frontend 自身 + 表示中の機体 backend** の両方
- 再起動対象は **起動中の pcbasm unit 全部**（`active_units()`。sudoers の argv をそのまま使う）

## 段階 1: 計画

### 2 の原因（特定済み）

`sudo -n systemctl restart --no-block <units>` は job を enqueue した時点で exit するが、
enqueue された restart job が **自分の cgroup**（`pcbasm-api.service`）を止めるほうが
先行しうる。すると `sudo`／`systemctl` の子プロセスが SIGTERM を受けて死に、
`Popen.returncode` が `-15` になる。`schedule_restart` はこれを「再起動コマンドの失敗」と
解釈して `report.fail()` を書くため、再起動後の画面に赤いエラーが残る。

→ 修正: `returncode < 0`（シグナル死）は「要求した再起動が先に自分を止めた」= 成功扱い。
`timed_out` は先に判定しているので巻き込まない。

### 公開インターフェース

`web/selfupdate/service.py`
- `unit_summary(units: tuple[str, ...]) -> str` 追加（`restart_notice` から抽出。表示文字列）
- `schedule_restart` の戻り値規約は不変（シグナル死を None にするだけ）

`web/selfupdate/settings.py`
- `UpdateSettings.watch_interval: float = 1800.0`（0 以下で無効）

`web/selfupdate/runner.py`
- `UpdateRunner.restart_services() -> str | None`（受理なら None、断る理由を返す）
- `UpdateRunner.start_watching() -> None` / `stop_watching() -> None`
- `UpdateRunner.refresh_remote() -> str | None`（通知のための fetch。`check()` の非対話版）

`web/api/models.py`
- `FirmwareRestartResponse(ok, message, warning, restart_units)`

`web/api/routers/system.py`
- `post_firmware_restart(...) -> FirmwareRestartResponse`（Klipper 送信 → 成功時のみ unit 再起動を予約）

`web/ui/models.py`
- `UpdateNoticeResponse(available, label, detail, href)`

`web/ui/update_api.py`
- `GET /api/update-notice?machine_id=<id>` → `UpdateNoticeResponse`
- `compose_notice(sources: Sequence[UpdateNoticeSource]) -> UpdateNoticeResponse`（純関数）

`web/ui/machine_client.py`
- `MachineClient.update_status() -> UpdateStatusResponse`

frontend: `base.html` に `#update-badge`、`static/js/update_notice.js`、`_chrome_context` に
`update_notice_url`。

### テスト観点

- schedule_restart: SIGTERM で死ぬ sudo スタブ → 理由 None（report が FAILED にならない）
- schedule_restart: 通常の非 0 終了は従来どおり理由を返す
- restart_services: 許可あり → sudo の restart 呼び出しがログに出る／sudoers 未設置 → 理由
- restart_services: 更新実行中は断る／unit 無しは何もせず None
- POST /api/firmware-restart: Klipper 不達は 502 のまま（再起動を予約しない）
- 通知: compose_notice の 0/1/N 件、backend 不達で落ちない、`/api/update-notice` の HTTP 契約
- watcher: 短い interval で origin の新 commit が plan に反映される

### リスク

- 再起動対象は sudoers 固定 argv と一致していること（`restart_command` を共有するので不変）
- 通知の fetch は 30 分周期。ネットワーク不通でも通知が壊れないこと（例外を出さない）

## 段階 3-5 の実施結果

### 計画外の判断（理由付き）

- **通知の重複畳み込み。** 同居機は frontend（`socket.gethostname()`）と backend
    （`resolve_machine_id` = `hostname or gethostname()`）が同名で同じ作業ツリーを見る。
    畳まないと 1 件の更新が「2 件」と表示されるため `compose_notice` でホスト名 dedupe を入れた
- **バッジに静的 href を置かない。** `tests/web/ui/test_pages.py::TestMachinePrefixedUrls`
    が「内部リンクは全て機体 prefix 付き」を要求するため、`href="/update"` を書くと落ちる。
    遷移先は機体ごとに変わるので JS がサーバ応答から入れる
- **firmware-restart は Klipper 送信成功後にのみサービスを再起動。** 逆順だと Moonraker
    不通のときに画面だけ落ちる
- **watcher の最初の fetch は 1 周期後。** 起動直後のネットワーク I/O でアプリ起動を遅らせない。
    既定 1800 秒なのでテストのアプリ起動では一度も fetch しない

### 自己レビューの指摘と対応

- `seen.add()` を内包表記の副作用に使っていた → 明示 for ループへ（可読性）
- docformatter が日本語 docstring を再折り返しして不自然な空白を挿入 → summary/本文を
    折り返し幅に収めて解消（memory: docformatter-cjk-wrap-corruption）

### 却下した案

- **通知のための fetch を status エンドポイント内で遅延実行する**: 毎秒ポーリングされる
    経路に最大 20 秒のネットワーク I/O が混じる。lifespan の watcher スレッドにした
- **firmware-restart を Klipper 不通でもサービス再起動する**: 装置が応答しない状態で
    画面まで消える。502 で中断する方が復旧しやすい

## code-reviewer の指摘と対応（verdict: request-changes → 対応済み）

- **M1 `returncode < 0` を一律成功扱い（must-fix）**: `run_command` は実行ファイル不在も
    `returncode=-1`（= `-SIGHUP`）で返すため、起動失敗がサイレントに成功になっていた。
    systemd が実際に使う `STOP_SIGNALS = {SIGTERM, SIGKILL}` に絞り、回帰テスト
    `test_missing_executable_is_still_a_failure` を追加
- **M2 `restart_services()` が flock を取らない（must-fix）**: `running` は自プロセスしか
    見ない。同居機の相方が `uv sync` 中でも unit を落とせた。更新と同じ flock を取り、
    再起動を投げ終えるまで握る。`self.running` の判定も `_guard` の中へ移動（TOCTOU）
- **S1 `schedule_restart` の戻り値を捨てていた**: `_restart_holding` で logger.warning
- **S2 確認ダイアログ無し**: 爆発半径が Klipper MCU から LAN 全体の画面に広がったので、
    `data-confirm` 属性（文言はテンプレート = サーバ側）で確認を挟む
- **S3 未使用フィールド**: `FirmwareRestartResponse` から `ok` / `restart_units` を削除
- **S4 ポーリング負荷**: バッジの取得周期を 60 秒 → 300 秒（サーバ側 fetch は 30 分周期
    なので細かく回しても新しい情報は増えない）
- **N1 表示文字列の空白退行**: `（unit）を再起動します` の空白を main と揃えた

未対応（理由付き）:

- **N2 blocker で止まっている機体は通知に出ない**: `update_available` の意味
    （押せば前へ進むか）を変える話なので別件。ユーザーに報告する
- **N3 dedupe は `PCBASM_HOSTNAME` 設定時に外れる**: 外れても 2 件表示になるだけで害が無い
- **N4 `restart_units()` のキャッシュ**: 既存挙動。変更するとキャッシュの意味が失われる
