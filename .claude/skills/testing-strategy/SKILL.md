---
name: testing-strategy
description: pcb-assembly のテスト戦略。実機を最優先・自前 HAL ABC のみ fake 可・3rd-party 表面 (picamera2 / libgpiod / Klipper RPC / OpenCV / time.sleep 等) のモック禁止。4 区分 (unit / integration-with-fakes / integration-hardware / e2e)、tests ミラーレイアウト (tests/web/api と tests/web/ui を含む)、書く/書かないリスト、公開 API 契約ピン例外、実 zeroconf と skip_if_no_mdns の扱い、@mark_hardware と make test-no-hardware の使い分け。テストコードを書く・壊れたテストを調査する・pytest 周りを設定する前に読む
---

# pcb-assembly テスト方針リファレンス

## 哲学

pcb-assembly はハードウェア装置制御が支配的なプロジェクト。「動くテスト」ではなく「**実機での振る舞いを保証するテスト**」を優先する。

ハードウェア表面をミラーした fake は自分の仮定をテストするだけで、ドライバ・配線・タイミングといった現実の障害要因を検出できない。実機障害を防ぐため、**実機テストを最優先**とする。一方で実機は常時 available ではないため、CI / 開発機では自前 HAL ABC の fake / 軽量代替で結合検証を回し、実機接続時に `@mark_hardware` テストで真の検証を行う、という二層構成で運用する。

## 検証対象の優先順位

1. **実機テスト (`@mark_hardware`)** — 真の検証。実カメラ (USB / CSI)、実 Klipper (シリアル / RPC)、実ロードセル、実 GPIO、実 PCB に対して走らせる。実 PNG を実 OpenCV / picamera2 / RapidOCR に通す。`make test` で実機接続時にローカル実行。skip 条件 (`skip_if_no_usb_camera` 等) で gating する。
2. **自前 HAL ABC の fake / 軽量代替** — `Camera`, `KlipperClient` など `src/pcbasm/hal/` で pcb-assembly が定義した抽象のみ fake してよい。`make test-no-hardware` の主体。ABC のみで具象が無い場合は `tests/helpers.py` に test 用 Impl を置く (`mocker.Mock` よりまず実 Impl を検討)。
3. **3rd-party 表面のモック → 禁止**: `picamera2.Picamera2`, `v4l2` ioctl, `libgpiod`, Klipper の Moonraker / klippy RPC, `cv2.*`, `time.sleep` など外部ライブラリの面を直接モックしない。仮定のミラーになり upstream の挙動変更を検出できない。
4. **内部関数モック → 禁止** — 自モジュール内の private 関数をモックしても、リファクタで壊れるだけで何の振る舞いも保証しない。

## 基本原則

- 必要十分なテストのみ記述。網羅率を稼ぐためのテストは書かない
- 内部実装ではなく公開インターフェースと振る舞いをテストする
- テスト関数に戻り値の型アノテーションは不要
- コードカバレッジは目標ではなく診断。100% は赤信号 (テストが弱い可能性)

## テストレイアウト

`tests/pcbasm/` が `src/pcbasm/` を 1 対 1 でミラーリング:

```
src/pcbasm/hal/camera.py       ↔ tests/pcbasm/hal/test_camera.py
src/pcbasm/vision/detection.py ↔ tests/pcbasm/vision/test_detection.py
src/pcbasm/geometry/polygon.py ↔ tests/pcbasm/geometry/test_polygon.py
```

- `tests/web/api/` が `src/web/api/`（backend WebAPI）を、`tests/web/ui/` が `src/web/ui/`（UI frontend）を同様にミラーする
- `tests/` 直下に置けるのは共通資産（`__init__.py` / `helpers.py` / `conftest.py`）と `e2e/`
    （実サーバー E2E）、それに **`src/` 側にミラー元が無い対象のテスト**だけ。後者は
    `test_package.py`（パッケージ構成）、`test_claude_hooks.py`（`.claude/` の hook）、
    `test_makefile_fake_targets.py`（`Makefile`）、`test_web_service_script.py`（`scripts/web-service.sh`）
    のようにリポジトリ資産そのものを検証するもので、ミラー先が無いから直下に置く
- `tests/helpers.py` が `mark_hardware` / `skip_if_no_*` / test 用 Impl の所在地。締め切り付き
    実行の `before_deadline`（ハングをテスト失敗に変える）、`wait_until`、
    `random_service_type` もここ
- 1 source ファイル 1 test ファイル原則。バックエンド分割があればテストも分割

WebUI は 2 プロセスなので、どちらのテストに置くかで検証範囲が変わる。

- `tests/web/api/` — backend 単体（`TestClient` で backend app を直に駆動）
- `tests/web/ui/` — frontend + 上流に実物の backend app（httpx `ASGITransport` で in-process）。
    **JSON プロキシと SSR ページだけ**書ける。MJPEG / WS / ジョブ実行は `ASGITransport` では
    成立しないので `tests/e2e/` へ（理由は skill `webui-e2e`）
- `tests/e2e/` — 実 uvicorn の backend + frontend を 2 プロセス起動して通しで叩く

## テスト 4 区分

| 区分                       | 配置                                        | 検証対象                                         | モック許容                     | 実行                           |
| -------------------------- | ------------------------------------------- | ------------------------------------------------ | ------------------------------ | ------------------------------ |
| **unit**                   | `tests/pcbasm/**/test_*.py`                 | 純粋ロジック (geometry の数学、PCB 配置パース等) | なし                           | 常時                           |
| **integration-with-fakes** | 同上 (`tests/helpers.py` の Impl を import) | モジュール間結合                                 | 自前 HAL ABC のみ              | 常時 (`make test-no-hardware`) |
| **integration-hardware**   | 同上、`@mark_hardware` 付与                 | adapter / 実機結合点                             | 実機、`skip_if_no_*` で gating | 実機接続時 (`make test`)       |
| **e2e**                    | `tests/e2e/`                                | WebUI の実サーバー通し動作 (backend + frontend)  | なし                           | 手動 (`make test-e2e`)         |

**hardware と非 hardware は同一テストクラス内に併存させる**。マーカー定義と接続確認手順は skill `hardware-test` を参照。

## 何をテストするか / しないか

### 書く

- 正常系、異常系、警告 (RuntimeWarning 等)、エッジケース
- 例外メッセージは **substring** で検証 (`assert "expected" in str(exc.value)`)

### 書かない

- 継承の追試: `class MyError(RuntimeError):` の `isinstance` 検証
- import 可能性の追試: `assert X is not None`
- 定数 literal の追試: `assert TIMEOUT == 5`
- getter / setter ラウンドトリップ
- framework / stdlib の動作追試
- **例外メッセージ完全一致**: 上記のとおり substring で十分
- モック戻り値をそのまま検証 (モックの設定をテストしているだけ)

### 例外: 公開 API 契約ピン

外部利用者が依存する API 名・基底クラス・型エイリアスは契約として固定してよい。まだ 1 件も無いので、最初に書く人が次を用意する:

- 集約場所: `tests/pcbasm/test_api_contract.py`
- マーカー: `@pytest.mark.api_contract`（未登録。下の「pytest 設定」に従って先に登録する）
- 対象例: `__all__` 整合性、公開例外の継承元、型エイリアスの解決先

## モック (使用する場合のルール)

- `pytest_mock` を使用 (`unittest.mock` は使わない、`mocker.Mock` / `mocker.patch`)
- 複数テストで共有するモックは `tests/conftest.py` のフィクスチャに集約
- **対象は自前 HAL ABC のみ**。3rd-party 表面と内部関数は対象外

### 実 zeroconf（mDNS）は「実オブジェクト検証」

mDNS の広告・探索テストは実 `zeroconf` を起動して実マルチキャストを通す。これは
**モック禁止則の例外ではなく、優先順位 1〜2 の「実物で検証する」側**である
（`zeroconf` をモックすると自分の仮定をミラーするだけで、TXT の欠損・解決タイムアウト・
サービス型の不一致といった実際の障害要因を検出できない）。

- `skip_if_no_mdns`（`tests/helpers.py`）は**実行時の能力プローブ**。5353 の共有 bind と
    ループバックへのマルチキャスト join を実際に試し、できない環境（一部コンテナ）で
    失敗ではなく skip にする。`skip_if_no_usb_camera` / `skip_if_no_csi_camera` と同じ構造で、
    「実物があるときだけ実物で検証する」ための gating
- 実 LAN を汚さないための隔離（サービス型のランダム化・ループバック限定・総件数で
    assert しない）は skill `webui-e2e` の「mDNS テストの隔離の鉄則」が正典。
    mDNS を触らないテストの fixture は `discovery_enabled=False` を既定にしてある

## pytest 設定

`pyproject.toml` の `[tool.pytest.ini_options]` で:

- `--strict-markers` が有効。登録済みのマーカーは `hardware` / `e2e` / `browser` だけ。新しいマーカーは同じ節の `markers` に登録してから使う（未登録だと収集時にエラー）
- `hardware` マーカーは `tests/helpers.py` の `mark_hardware = pytest.mark.hardware` 経由で付与する (テスト側で文字列リテラル `@pytest.mark.hardware` を直接書かない)

## 関連 skill / memory

- `hardware-test` — `mark_hardware` / `skip_if_no_*` の定義と実機接続確認手順
- `refactor-conventions` — `class TestXxx:` 構造、private prefix、None 返却バリデーションなどテストコードの書き方
- [memory/feedback_no_private_test.md](../../../memory/feedback_no_private_test.md) — private を直接テストしない
- [memory/feedback_test_class.md](../../../memory/feedback_test_class.md) — テストはクラスに集約
- [memory/feedback_no_try_catch.md](../../../memory/feedback_no_try_catch.md) — None 返却バリデーション

## 参考文献

- Fowler: Testing shapes (pyramid / honeycomb / trophy)
- GOOS 原則: 自分が所有しているもののみ mock する
