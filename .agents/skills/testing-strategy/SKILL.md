---
name: testing-strategy
description: pcb-assembly のテスト戦略。pytest は実機に触れず、実機の確認は受け入れ試験（docs/acceptance.md）が持つ。5 区分（unit / fake を使う結合 / golden / e2e / 受け入れ試験）、tests のレイアウト（ミラー、support、data、golden、repo）、fake してよいのは自前 HAL の fake だけで 3rd-party 表面（picamera2 / Klipper RPC / OpenCV / time.sleep 等）と内部関数のモックは禁止、fail-closed の fixture、書く/書かないリスト、実 zeroconf と skip_if_no_mdns の扱い。テストコードを書く・壊れたテストを調査する・pytest 周りを設定する前に読む
---

# pcb-assembly テスト方針リファレンス

## 哲学

テストは仕様書である。公開インターフェースと観測可能な振る舞いを、実データ・実オブジェクトで確かめる。

pytest は実機に触れない。テストは本番機の上でも走るので、root conftest がテストごとに実機への経路を塞ぐ（下の「fail-closed」）。
装置へ送る G-code の列は golden で固定し、golden で見えないもの（機構の動き、カメラ、音、画面）は受け入れ試験（`docs/acceptance.md`）で人が実機で確かめる。

## 5 区分

| 区分                | 配置                              | 検証対象                                                      | 実行                     |
| ------------------- | --------------------------------- | ------------------------------------------------------------- | ------------------------ |
| **unit**            | `tests/pcbasm/**`、`tests/web/**` | 計算、ファイル、SSR / JSON などの振る舞い                     | `make check`             |
| **fake を使う結合** | 同上（`pcbasm.hal.fake` を使う）  | モジュール間の結合、ジョブの流れ                              | `make check`             |
| **golden**          | `tests/golden/`                   | ジョブが送る G-code 列と実効 timeout、成果物 JSON、基板の署名 | `make check`             |
| **e2e**             | `tests/e2e/`                      | 実 uvicorn の backend + frontend と Chromium の通し動作       | `make test-e2e`          |
| **受け入れ試験**    | `docs/acceptance.md`              | 実機での機構・カメラ・音・画面                                | merge 前に人が実機で行う |

`make check` は `make format`、`make type`、`make test` を順に流す。テンプレートや JS に触れたら `make test-e2e` も流す（skill [webui-e2e](../webui-e2e/SKILL.md)）。

## モックと fake

1. **実物を優先する**：実 PNG を実 OpenCV に通す、実ファイルを読み書きする、実 zeroconf を起動する
2. **fake してよいのは自前 HAL だけ**：`pcbasm.hal.fake` の `FakeKlipper`、`SceneCamera`、`FakeAudioPlayer` などを使う。`make dev` と golden も同じ fake を使う
3. **3rd-party 表面のモックは禁止**：`picamera2.Picamera2`、v4l2、Klipper の Moonraker RPC、`cv2.*`、`time.sleep` などを直接モックしない。モックはテスト作成者の仮定を写すだけで、upstream の挙動変更を検出できない
4. **内部関数のモックは禁止**：private 関数をモックしても、リファクタのたびに落ちるだけで振る舞いは何も保証しない

モックが要るときは `pytest_mock`（`mocker`）を使い、`unittest.mock` は使わない。

## fail-closed

root conftest（`tests/conftest.py`）の autouse fixture が、テストから実機とサービスへの経路を塞ぐ。外さない。

- `forbid_real_klipper`：`open_klipper` を呼ぶとテストが落ちる。Klipper が要るテストは `moonraker`（`tests/support/moonraker.py` の FakeMoonraker）か `mount_klipper`（`FakeKlipper`）を使う
- `forbid_real_systemctl`：自己更新の `sudo` と `systemctl` を、呼ばれたら失敗する stub に向ける
- `fake_by_default`：`PCBASM_FAKE=1` にし、env から組む起動設定を fake の機体にする

この契約は `tests/repo/test_fail_closed.py` が固定する。

## golden

ジョブの golden は `tests/golden/` に置き、fake の機体の上で実 Backend と実 JobManager を走らせて採る（`tests/golden/conftest.py`）。

- 比べ方は `tests/support/golden.py`：命令と語の並びは完全一致、数値は |Δ| ≤ 1e-9
- 装置を動かすジョブを catalog に足したら、`tests/golden/scenarios.py` にシナリオを足す（`test_coverage.py` が落ちる）
- golden を変えるのは、送る列を意図して変えるときだけ。`PCBASM_UPDATE_GOLDEN=1` で書き換え、差分の理由を PR 本文に書く

## テストレイアウト

`tests/` は `src/` を 1 対 1 でミラーする。1 source ファイル 1 test ファイルが原則。

```
src/pcbasm/vision/detection.py ↔ tests/pcbasm/vision/test_detection.py
src/web/api/routers/session.py ↔ tests/web/api/routers/test_session.py
src/web/ui/pages.py            ↔ tests/web/ui/test_pages.py
```

ミラー以外の置き場所は次のとおり。

| 場所                         | 置くもの                                                                    |
| ---------------------------- | --------------------------------------------------------------------------- |
| `tests/conftest.py`          | 全テスト共通の fixture だけ（fail-closed を含む）                           |
| `tests/support/`             | テストの支援部品（fake の Moonraker、待ち合わせ、web / browser の起動など） |
| `tests/data/`                | テストの入力（設定、画像、KiCad の基板など）                                |
| `tests/golden/`              | golden とそれを採るシナリオ                                                 |
| `tests/e2e/`                 | 実サーバーの E2E                                                            |
| `tests/repo/`                | ミラー元の無いリポジトリ資産の契約（Makefile、install.sh、更新経路など）    |
| `tests/test_architecture.py` | src の import が層の規則に従うこと                                          |

WebUI は 2 プロセスなので、どちらのテストに置くかで検証範囲が変わる。

- `tests/web/api/`：backend 単体（`TestClient` で backend app を直に駆動）
- `tests/web/ui/`：frontend + 上流に実物の backend app（httpx `ASGITransport` で in-process）。
    **JSON プロキシと SSR ページだけ**書ける。MJPEG / WS / ジョブ実行は `tests/e2e/` へ（理由は skill `webui-e2e`）

待ち合わせは `tests/support/polling.py` の `wait_until` と `before_deadline` を使う。固定値の `sleep` で待たない。

## 何をテストするか / しないか

### 書く

- 正常系、異常系、警告（RuntimeWarning 等）、エッジケース
- 例外メッセージは **substring** で検証する（`assert "expected" in str(exc.value)`）
- テストは `class TestXxx:` にまとめ、テスト関数名を仕様の 1 行として読める形にする

### 書かない

- 継承の追試：`class MyError(RuntimeError):` の `isinstance` 検証
- import 可能性の追試：`assert X is not None`
- 定数 literal の追試：`assert TIMEOUT == 5`
- getter / setter のラウンドトリップ
- framework / stdlib の動作追試
- 例外メッセージの完全一致
- モックの戻り値をそのまま検証すること（モックの設定をテストしているだけ）
- private（`_` prefix）の直接テスト

## 実 zeroconf（mDNS）

mDNS の広告・探索テストは実 `zeroconf` を起動して実マルチキャストを通す。モックすると TXT の欠損・解決タイムアウト・サービス型の不一致を検出できない。

- `skip_if_no_mdns`（`tests/support/web.py`）は実行時の能力プローブ。5353 の共有 bind とループバックへのマルチキャスト join を試し、できない環境（一部コンテナ）では失敗ではなく skip にする
- 実 LAN に影響を与えないための隔離（サービス型のランダム化・ループバック限定・総件数で assert しない）は skill `webui-e2e` の「mDNS テストの隔離の鉄則」が正典。mDNS を触らないテストの fixture は `discovery_enabled=False` を既定にしてある

## pytest 設定

- `pyproject.toml` の `--strict-markers` が有効。登録済みのマーカーは `e2e` / `browser` だけ。新しいマーカーは同じ節の `markers` に登録してから使う
- `tests/e2e/` には conftest が `e2e` を自動で付け、`make test` は `-m "not e2e"` で除外する

## 関連 skill / memory

- `refactor-conventions`：`class TestXxx:` 構造、private prefix、None 返却バリデーションなどテストコードの書き方
- `webui-e2e`：E2E の書き方と実行
- [memory/feedback_no_private_test.md](../../../memory/feedback_no_private_test.md)：private を直接テストしない
- [memory/feedback_test_class.md](../../../memory/feedback_test_class.md)：テストはクラスに集約
- [memory/feedback_no_try_catch.md](../../../memory/feedback_no_try_catch.md)：None 返却バリデーション
