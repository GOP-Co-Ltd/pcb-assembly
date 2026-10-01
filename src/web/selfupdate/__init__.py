"""WebUI からのソフトウェア更新（git pull → uv sync → サービス再起動）.

`pcbasm` ではなく `web` の下に置く。これはデプロイ運用であって装置ドメインではなく、
`pcbasm` に `pcbasm-api.service` や `uv` を知らせたくない。backend（`web.api`）と
UI frontend（`web.ui`）の双方から同じ実装を使う。

このパッケージは cv2 / pcbnew / picamera2 を一切 import しない
（`tests/test_package.py::TestLightweightImports` で回帰を検出する）。frontend
専用機には装置ドメインの依存が入っていないため。
"""

from web.selfupdate.report import UpdatePlan, UpdateReport, UpdateState, UpdateStep
from web.selfupdate.runner import UpdateRunner
from web.selfupdate.settings import UpdateSettings

__all__ = [
    "UpdatePlan",
    "UpdateReport",
    "UpdateRunner",
    "UpdateSettings",
    "UpdateState",
    "UpdateStep",
]
