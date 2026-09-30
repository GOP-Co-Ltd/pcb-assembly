# アーキテクチャ

[ドキュメント一覧](../README.md) · [開発への参加](../CONTRIBUTING.md)

装置制御の Python コアと、2 つの FastAPI プロセスで構成する。
機体固有の状態は backend に集め、frontend は LAN 上の複数 backend を切り替えて表示する。

```mermaid
flowchart TD
    browser[ブラウザ] --> ui[web.ui :8080]
    ui -->|HTTP / WebSocket / MJPEG| api[web.api :8081・機体ごと]
    api --> jobs[ジョブ・設定・状態]
    jobs --> core[pcbasm・計算と装置の手順]
    core --> hal[HAL・カメラ / Klipper / 音声]
    hal --> device[機体]
```

## 責務と変更先

| 配置                                | 責務                                                         |
| ----------------------------------- | ------------------------------------------------------------ |
| `src/pcbasm/geometry/`              | 座標変換、ポリゴン、経路、配置の計算                         |
| `src/pcbasm/pcb/`                   | KiCad 読込・生成、パッド、footprint、単位変換                |
| `src/pcbasm/vision/`                | カメラ校正、画像・銅箔・塗布点の検出                         |
| `src/pcbasm/hal/`                   | カメラ、Klipper、ステージ、ディスペンサー、音声の抽象と実装  |
| `src/pcbasm/posctrl/`               | 基板の位置合わせ、基準点、補正、カメラと装置の座標対応       |
| `src/pcbasm/pasting/`               | 塗布の設定解決、経路、高さ面、機械手順、画像収集、塗布量校正 |
| `src/pcbasm/visualization/`         | PCB・塗布経路・高さ面・校正結果の描画                        |
| `src/pcbasm/pnp/`                   | 部品実装向けの予約された名前空間。未実装                     |
| `src/web/api/routers/`              | HTTP 入出力とコア呼び出し、例外から HTTP 応答への変換        |
| `src/web/api/jobs/`                 | 長時間処理、進捗、中断、操作者への質問、成果物の公開         |
| `src/web/ui/`                       | マシン探索・選択、SSR ページ、backend への中継               |
| `src/web/ui/templates/` / `static/` | HTML、CSS、DOM 操作、API 応答の表示                          |
| `src/web/selfupdate/`               | backend / frontend 共通の Git 更新とサービス再起動手順       |
| `scripts/`                          | ホストのセットアップとサービス運用                           |

`pcbasm` は `web` を import しない。ジョブの画面や API が変わっても、
位置合わせや塗布計画の計算はコア層で検証できるようにする。
塗布ドメイン内の詳細は [pasting の構成](../src/pcbasm/pasting/README.md) を参照する。

## リクエストとジョブ

ページは `/m/{machine_id}/…`、機体の API は `/m/{machine_id}/api/…` を使う。
frontend の `MachineClient` が backend から表示用のモデルを取得し、
`pages.py` が `layout.py` の表示定義に従ってテンプレートへ渡す。

装置の状態、実効設定値、集計値は backend が返す。
frontend と JS はそれらを再計算しない。たとえばパッドの設定を編集したら、
サーバーが解決した応答か再取得した値で画面を更新する。

ジョブの公開名とパラメータは backend のカタログが管理する。
装置への変更操作は backend の操作権で調停され、閲覧者も緊急停止と中止を実行できる。
UI のボタン無効化だけに依存しない。

実行中の編集・対話コマンド・結果の反映や破棄には、ブラウザが表示しているジョブの
`expected_job_id` を渡し、backend が対象の一致を確認する。
ジョブが切り替わったら未送信の編集を破棄し、次のジョブへ持ち越さない。

## 機能を足すときの変更先

計算と装置の手順は先に `src/pcbasm/` へ置き、web 側には呼び出しと表示だけを足す。

### 機体の API を足す

1. `src/web/api/routers/` に router を書く。パスは `/api/` で始める。
2. `src/web/api/app.py` の `include_router` に router を登録する。
3. 装置や設定を変える endpoint には `ControlDep` を付ける。操作権のない要求は 423 になる。
4. 応答は `src/web/api/models.py` の pydantic モデルで返す。

frontend 側の変更は要らない。`proxy.py` が `/m/{machine_id}/api/**` をそのまま backend の `/api/**` へ中継する。
JS からは `static/js/app.js` の `api()` か `fetchApi()` に `/api/…` のパスを渡す。
JS に `fetch()` を直接書かない。機体の prefix が付かず、テストでも禁止している。

### ジョブを足す

ジョブ関数と登録の場所はタブで決まる。

| タブ    | ジョブ関数と登録の場所                                                                                                |
| ------- | --------------------------------------------------------------------------------------------------------------------- |
| dev     | `src/web/api/jobs/dev.py` の `register_dev_jobs`                                                                      |
| posctrl | `src/web/api/jobs/posctrl.py` の `register_posctrl_jobs`                                                              |
| pasting | `src/web/api/jobs/pasting/` に新しいモジュールと `register` を作り、`__init__.py` の `register_pasting_jobs` から呼ぶ |

1. ジョブ関数を書く。関数は `JobContext` を受け取り、`pcbasm` を呼ぶ。ログ・進捗・質問は `JobContext` の `log` / `progress` / `prompt` で出す。
2. `JobDefinition` を登録する。`name` は feature slug と同じ文字列にする。装置を動かさないジョブは `uses_machine=False` にする。
3. 次節「ページを足す」の手順で、ジョブのページを足す。`hidden` でないジョブがページに載っていないとテストが落ちる。

ジョブの開始・中止・結果の反映は `routers/jobs.py` の共通 API が扱う。ジョブごとの router は要らない。

### ページを足す

ページの URL は `/m/{machine_id}/{tab}/{feature}` になる。
`src/web/ui/layout.py` に登録していない feature は 404 になる。

1. `layout.py` の `FEATURE_GROUPS` で、該当タブのグループに feature slug を足す。サイドバーと入口画面に表示される。
2. `layout.py` の `FEATURE_TEMPLATES` に `(tab, slug)` とテンプレート名を足す。テンプレートは `src/web/ui/templates/` に置く。
3. ページの種類に応じて、表示名とテンプレートを決める。

| ページの種類 | 表示名                                  | テンプレート                                                                                                     |
| ------------ | --------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| ジョブ       | backend の `JobDefinition.label` を使う | `JOB_TEMPLATES` に含まれるもの。汎用の `job.html`（全タブ共用）、`pasting/job.html`、`posctrl/job.html` で足りる |
| ジョブ以外   | `layout.py` の `FEATURE_LABELS` に足す  | `JOB_TEMPLATES` に入れない。入れると backend にジョブが無いため 503 になる                                       |

ページに追加の値が要るときは、次の順で足す。

1. backend の応答に無い値は、frontend で計算せず、backend の応答モデルに足す。
2. `pages.py` の `_FEATURE_CONTEXT` か `_JOB_FEATURE_CONTEXT` に、backend の応答からその値をテンプレートへ渡す処理を足す。

## 設定とデータの所有者

| データ                                  | 既定の場所                          | 所有者                                      |
| --------------------------------------- | ----------------------------------- | ------------------------------------------- |
| 機体の設定・カメラ校正                  | `config/`                           | backend。Git 管理外                         |
| Klipper 設定の実体                      | `~/printer_data/config/printer.cfg` | Klipper。`config/printer.cfg` はリンク      |
| 静的な機体登録                          | `config/machines.toml`              | frontend が起動時に読む                     |
| 選択 PCB・基板別 override・ジョブ成果物 | `data/webui/`                       | backend                                     |
| 塗布画像データセット                    | `data/paste-volume-datasets/`       | backend                                     |
| 塗布量校正                              | `data/paste-volume-calibrations/`   | backend。再現に必要な校正資産               |
| 自己更新の記録・ロック                  | `data/selfupdate/`                  | 同じ worktree の backend と frontend が共有 |

PCB の閲覧・アップロード先も backend 機のファイルシステムである。
frontend 専用ホストは機体の `machine.toml` を読まない。
環境変数による場所の変更は [WebUI の環境変数](webui.md#%E7%92%B0%E5%A2%83%E5%A4%89%E6%95%B0) を参照する。

永続ファイルの構造を変える場合は既存データの読込・移行を扱う。
ディレクトリ名だけを変更すると運用データが孤立するため、見た目だけの改名は行わない。
`data/testing/` のテスト資産と `data/config-templates/` の初期設定を実機データと混同しない。
