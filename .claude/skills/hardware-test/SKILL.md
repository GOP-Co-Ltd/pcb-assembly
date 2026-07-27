---
name: hardware-test
description: PCBアセンブリのハードウェアテスト記述・実行手順。@mark_hardware／skip_if_no_*camera の使い分け、pytest -m hardware の実行、v4l2-ctl での接続確認まで。GPIOやカメラ、ステージなど物理デバイス絡みのテストを書く／走らせるときに参照する。
---

# ハードウェアテスト手順

物理デバイス（USB/CSIカメラ、Klipper接続のステージ・サーボ・エアポンプ、GPIO等）を伴うテストの記述と実行のための skill。

## マーカーと skip 条件

`tests/helpers.py` で以下を提供：

| 名前                    | 用途                                                          |
| ----------------------- | ------------------------------------------------------------- |
| `mark_hardware`         | `pytest.mark.hardware` のエイリアス。ハードウェアテストに付与 |
| `skip_if_no_usb_camera` | USBカメラ（uvcvideo）非接続時にスキップ                       |
| `skip_if_no_csi_camera` | CSIカメラ（picamera2）非接続時にスキップ                      |

### 使用例

```python
from tests.helpers import mark_hardware, skip_if_no_usb_camera


class TestCameraCapture:
    def test_with_mock(self, mock_camera_backend):
        # モックカメラでの振る舞いテスト
        ...

    @mark_hardware
    @skip_if_no_usb_camera
    def test_with_real_camera(self):
        # 実機USBカメラでのテスト
        ...
```

## テストクラス内での分離パターン

同一テストクラス内に「モック版」と「ハードウェア版」を併存させる。

- モック版：`mock_camera_backend` などのフィクスチャ利用、CIで実行
- ハードウェア版：`@mark_hardware` 付与、実機接続時のみ実行

## 接続確認コマンド

```bash
# USBカメラ確認
v4l2-ctl --list-devices

# CSIカメラ確認（Python経由）
uv run python -c "import picamera2; print(picamera2.Picamera2.global_camera_info())"
```

## テスト実行コマンド

agent が実行してよいのはこれだけ:

```bash
# ハードウェアテストを除外
make test-no-hardware
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

- 新しい skip マーカー（例：`skip_if_no_klipper`）を追加する場合は `helpers.py` に定義し、テストから import
- マーカー判定関数は `_` prefix の private（例：`_usb_camera_available`）
- import 時の副作用（ハードウェア初期化）が起きないよう、判定は遅延評価できる構造に
