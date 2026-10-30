# 開発の手引き

[ドキュメント一覧](../../README.md) · [アーキテクチャ](architecture.md) · [実機受け入れ試験](../acceptance.md)

不具合報告、使い勝手の改善、ドキュメントの修正も歓迎する。
変更は解決したい問題と確認方法を明確にし、レビューできる大きさにまとめる。

## 不具合を報告する

GitHub の Issue に、次の情報を添える。

- 期待する動作と実際の動作、再現する操作順
- 対象の画面・ジョブ、機体の機能（`machine.toml` の `[solder]` / `[pnp]`）、OS と Python のバージョン
- `git rev-parse --short HEAD` の結果と、backend / frontend が同じ版か
- エラーメッセージと関係するログ。UI の問題は画面幅やスクリーンショットも有用

ログや設定を共有するときは、認証情報、社内ホスト名、共有できない基板データを取り除く。
装置の移動が関係する問題は、実機で再現させずに分かっている範囲を記載してよい。

## 機体の機能

機体ははんだ塗布機と部品実装（Pick and Place）機があり、両方を持つ機体もある。
どれかは `machine.toml` の節で決まり、`[solder]` があれば塗布、`[pnp]` があれば部品実装を使える。
ジョブ・API・画面・立ち上げの段は、この機能で分かれる。
仕組みと、PnP を足すときの変更先は [アーキテクチャ](architecture.md#%E6%A9%9F%E4%BD%93%E3%81%AE%E6%A9%9F%E8%83%BD%E3%81%AF-machinetoml-%E3%81%AE%E7%AF%80%E3%81%A7%E8%A1%A8%E3%81%99) にある。

## 開発環境

このリポジトリの全体検証は Raspberry Pi OS の環境を前提とする。
`pcbnew` は KiCad、`picamera2` は OS パッケージから提供される。
Python は `pyproject.toml` で 3.12 以上を要求し、型チェック・整形・CI は
システム Python 3.13 を対象とする。

環境は [導入](../install.md) の手順で作る。
`./install.sh` の packages 段が Git LFS の実体を取得し、python 段が `make setup` まで行う。

| したいこと                          | コマンド                        |
| ----------------------------------- | ------------------------------- |
| `.venv` を作り直す                  | `make setup`                    |
| 既存の `.venv` へ依存関係を同期する | `uv sync --locked --all-groups` |

`make setup` は既存の `.venv` を消して作り直す。
venv は `--system-site-packages` が必要で、uv の `python-preference = "only-system"`
を維持する。Pi 向け OS パッケージがない PC では全テストを collect できない。

## 変更と PR

```sh
git fetch origin
git switch -c fix/YYYY-MM-DD/short-description origin/main
```

ブランチ名は `<種別>/<YYYY-MM-DD>/<短い説明>` とし、`origin/main` から作る。
種別は `feature` / `fix` / `refactor` / `docs` / `chore` のどれか。
コミットは英語で `<type>(<scope>): <description>` とし、type にはブランチと同じ種別を使う。
1 コミットを 1 つの関心事にする。
例: `fix(web): keep machine controls visible on small screens`。

1. 再現条件または成功条件を先に決める。
2. 必要な範囲を実装し、公開インターフェースを使って動作を確かめる。
3. 下記の検証を通してからコミットする。
4. PR テンプレートに沿って、問題、変更後の動作、検証コマンドと結果、実施した受け入れ試験、未確認の範囲を書く。

main に直接コミットしない。main が進んでいる場合は作業ブランチへ
`git merge origin/main` で取り込み、検証する。push 済みの履歴は書き換えない。
積み重ねる PR は直前のブランチを base に指定し、依存する PR を説明にリンクする。
main へのマージはメンテナーが判断する。

## 検証の選び方

コミット前に次の基本検証を実行する。

```sh
make check  # make format、make type、make test を順に実行する
```

WebUI の画面・HTTP・WebSocket・動画配信を変更したら `make test-e2e` も実行する。
Chromium がシステムにない場合は `make playwright-install` でブラウザを用意する。

| 検証               | 用途                                              | 実行例                                |
| ------------------ | ------------------------------------------------- | ------------------------------------- |
| テスト             | 計算、ファイル、HAL fake を通す結合、SSR / JSON   | `make test`                           |
| 対象を絞ったテスト | 修正中の短い検証                                  | `uv run pytest tests/pcbasm/geometry` |
| E2E                | 隔離した実サーバーを通すブラウザ・HTTP・WS・MJPEG | `make test-e2e`                       |
| 実機受け入れ試験   | 接続された装置での動作。担当者が merge 前に実施   | [acceptance.md](../acceptance.md)     |

テストは実機に触れない。Klipper への接続と systemctl の呼び出しは、テスト中は必ず失敗する。

`make format` は git が追跡するファイルだけを検査する。新規ファイルは `git add` してから実行する。
`make format` がファイルを書き換えた場合は diff を確認して再実行する。
日本語 docstring は 1 文を 1 行に書く。複数行にまたがる文は docformatter が崩す。
GitHub Actions は専用 Raspberry Pi runner で pre-commit、pyright、テスト、隔離 E2E を
別 job にして並列に実行する。`pytest` job は非 E2E テスト、`e2e` job はブラウザ・HTTP・WS・
MJPEG の検証を受け持ち、JUnit レポートを `pytest-report` と `e2e-report` の artifact に残す。

## テストの書き方

- `tests/pcbasm/` と `tests/web/` は `src/` のモジュール配置に合わせる。
    実サーバーが必要な検証は `tests/e2e/` に置く。
- `class TestXxx` に集約し、同じ振る舞いの入力違いは `pytest.mark.parametrize` にする。
- 公開 API の入出力と観測可能な振る舞いを検証する。private 属性や呼出順を固定しない。
- 実データと一時ファイルを優先する。fake は自前 HAL の抽象だけに使い、
    OpenCV、picamera2、Klipper RPC、`time.sleep`、内部関数をモックしない。
- 実機でしか確かめられないことはテストにせず、[実機受け入れ試験](../acceptance.md) に項目を足す。
- getter の追試や定数の複写だけのテストを増やさない。失敗がどの回帰を示すかを説明できる内容にする。

JSON / SSR は in-process の実 backend で検証できる。
WebSocket・MJPEG・ジョブ実行は `tests/e2e/conftest.py` の `live_ui` / `live_server`
を使う。fixture が起動・停止を管理し、config とデータを一時ディレクトリへ隔離する。
mDNS の検証はランダムなサービス型とループバックだけを使う。

詳しい規約は [テスト戦略](../../.claude/skills/testing-strategy/SKILL.md) と
[WebUI E2E](../../.claude/skills/webui-e2e/SKILL.md) を参照する。

## 文書を直す

文書は利用者の旅程の順に並べている（[文書の地図](../../README.md#%E6%96%87%E6%9B%B8%E3%81%AE%E5%9C%B0%E5%9B%B3)）。コードの表を写した文書は、テストが食い違いを見つける。

| 文書                                           | 照らし合わせる相手                     | テスト                               |
| ---------------------------------------------- | -------------------------------------- | ------------------------------------ |
| [立ち上げ](../commissioning.md) の表           | `pcbasm.commissioning.STEPS`           | `tests/pcbasm/test_commissioning.py` |
| [設定キー一覧](../reference/config.md)         | 設定ページの項目（`setting_fields()`） | `tests/repo/test_docs.py`            |
| [アーキテクチャ](architecture.md) の機能の地図 | `web.ui.features.FEATURES`             | `tests/repo/test_docs.py`            |
| 文書の相対リンク                               | リポジトリのファイル                   | `tests/repo/test_docs.py`            |

設定キー一覧は手で直さない。設定のスキーマを変えたら `uv run python -m tests.support.config_reference > docs/reference/config.md` の後に `make format` を実行する。
行数や件数のように、コードを変えるたびに古くなる数値は文書に書かない。

## 実機設定を使わずに画面を確認する

自動確認はまず `make test-e2e` を使う。手動でブラウザを開く場合は `make dev` を使う。

```sh
make dev
```

Klipper もカメラも fake にした backend（port 8099）と frontend（port 8098）が同時に起動するので、
`http://127.0.0.1:8098` を開く。ステージは送った G-code のとおりに動き、カメラはその位置から
選択中の基板（4 隅の基準円と TOP 銅箔）と塗布痕を描く。jog や、はんだ塗布などの装置を使うジョブも画面から通せる。

設定は `data/sim/config` を一時ディレクトリへ複製して使い、`Ctrl-C` で終了すると設定の変更・ジョブの成果物ごと消える。
mDNS とソフトウェア更新は無効になる。ポートは `PCBASM_API_PORT` と `PCBASM_UI_PORT` で変えられる。

## 実装の責務

計算・装置の手順は `src/pcbasm/` に置く。WebAPI router は入出力変換を行い、
frontend は backend が返した値を表示する。JS に設定の継承解決や幾何計算を複製しない。
内部属性は原則 `_` prefix とし、既存の公開インターフェースを維持する。
入力の検証は値オブジェクトの `validate() -> str | None` を基本にする。

責務ごとの配置と機能からコードへの地図は [アーキテクチャ](architecture.md)、詳細は [CLAUDE.md](../../CLAUDE.md)
と [リファクタリング規約](../../.claude/skills/refactor-conventions/SKILL.md) を参照する。
