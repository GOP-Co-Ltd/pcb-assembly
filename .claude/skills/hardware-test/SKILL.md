---
name: hardware-test
description: PCB アセンブリのハードウェアテスト記述・実行手順。@mark_hardware／skip_if_no_*camera の使い分け、pytest -m hardware の実行、v4l2-ctl での接続確認まで。GPIO やカメラ、ステージなど物理デバイス絡みのテストを書く／走らせるときに参照する。
---

# ハードウェアテスト手順

物理デバイス（USB/CSI カメラ、Klipper 接続のステージ・サーボ・エアポンプ、GPIO 等）を伴うテストの記述と実行のための skill。

## マーカーと skip 条件

`tests/helpers.py` は次のマーカーと skip 条件を提供する。

| 名前                    | 用途                                                                       |
| ----------------------- | -------------------------------------------------------------------------- |
| `mark_hardware`         | `pytest.mark.hardware` のエイリアス。ハードウェアテストに付与              |
| `skip_if_no_usb_camera` | USB カメラ（uvcvideo）非接続時にスキップ                                   |
| `skip_if_no_csi_camera` | CSI カメラ（picamera2）非接続時にスキップ                                  |
| `skip_if_no_alsa_audio` | ALSA 再生デバイス（`aplay -l`）が無いときにスキップ                        |
| `skip_if_no_mdns`       | mDNS が使えない環境でスキップ。実機ではないので `mark_hardware` は付けない |

### 使用例

```python
from tests.helpers import FakeCamera, mark_hardware, skip_if_no_usb_camera


class TestCameraCapture:
    def test_with_fake(self):
        camera = FakeCamera(images)  # 自前 HAL の fake で振る舞いをテスト
        ...

    @mark_hardware
    @skip_if_no_usb_camera
    def test_with_real_camera(self):
        # 実機USBカメラでのテスト
        ...
```

## テストクラス内での分離パターン

同一テストクラス内に「fake 版」と「ハードウェア版」を併存させる。

- fake 版：`tests/helpers.py` の `FakeCamera` / `FakeKlipper` / `FakeAudioPlayer`（自前 HAL の fake）を使い、常時実行
- ハードウェア版：`@mark_hardware` 付与、実機接続時のみ実行

## 接続確認コマンド

```bash
# USBカメラ確認
v4l2-ctl --list-devices

# CSIカメラ確認（Python経由）
uv run python -c "import picamera2; print(picamera2.Picamera2.global_camera_info())"
```

## テスト実行コマンド

agent が実行してよいのは次のコマンドだけ。

```bash
# ハードウェアテストを除外
make test-no-hardware

# 範囲を絞るときも -m "not hardware" を必ず付ける（対象パスに関係なく）
uv run pytest tests/pcbasm/hal/ -m "not hardware"
```

以下は**ユーザーが手元で実行する**コマンド。実機が動くため agent は実行しない（`make test` は `settings.json` で deny 済み）。

```bash
# すべて実行
make test

# ハードウェアテストのみ
uv run pytest -m hardware

# 特定モジュールのみ
uv run pytest tests/pcbasm/hal/ -m hardware
```

## `tests/helpers.py` 拡張時の注意

- 新しい skip マーカー（例：`skip_if_no_klipper`）は `helpers.py` に定義し、テストから import する
- 判定関数は `_` prefix の private にする（例：`_usb_camera_available`）
- マーカーは `_skip_unless_available(判定関数, skip 理由)` で作る。判定はテスト実行時に走るので、import 時にハードウェアを初期化しない
