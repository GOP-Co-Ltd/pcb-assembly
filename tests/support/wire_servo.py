"""疑似端末の master 側で STS のサーボを演じる子プロセス（``tests/pcbasm/hal/test_servo.py`` が起動する）.

pytest と同じプロセスのスレッドで演じると、scservo_sdk の受信待ち（空回りの読み出し）と GIL を取り合って
応答が遅れ、SDK の待ち時間（約 34 ms）を超えることがある。CI の self-hosted runner のように負荷の高い
ホストで不安定になるので、別プロセスにする。

使い方: ``python wire_servo.py <master の fd> <reply_delays の JSON>``。標準ライブラリだけを使う。

- 起動したら ``ready`` の 1 行を stdout へ書く
- 受けた instruction packet は、応答を返す前に hex の 1 行で stdout へ書く
- 読み出しには 0 を並べて返す。ブロードキャスト（ID 0xFE）には応答しない
- reply_delays（アドレス → 秒）にあるアドレスへの WRITE は、その秒数だけ遅れて応答する
"""

from __future__ import annotations

import json
import os
import sys
import time

_INSTRUCTION_READ = 0x02
_INSTRUCTION_WRITE = 0x03
_BROADCAST = 0xFE


def _serve(master: int, reply_delays: dict[int, float]) -> None:
    buffer = b""
    while True:
        try:
            chunk = os.read(master, 256)
        except OSError:
            return
        if not chunk:
            return
        buffer += chunk
        while len(buffer) >= 4 and len(buffer) >= buffer[3] + 4:
            size = buffer[3] + 4
            packet, buffer = buffer[:size], buffer[size:]
            print(packet.hex(), flush=True)
            if packet[2] == _BROADCAST:
                continue
            if packet[4] == _INSTRUCTION_WRITE:
                time.sleep(reply_delays.get(packet[5], 0.0))
            data = bytes(packet[6]) if packet[4] == _INSTRUCTION_READ else b""
            body = bytes([packet[2], len(data) + 2, 0]) + data
            os.write(master, b"\xff\xff" + body + bytes([~sum(body) & 0xFF]))


if __name__ == "__main__":
    delays = {int(key): value for key, value in json.loads(sys.argv[2]).items()}
    print("ready", flush=True)
    _serve(int(sys.argv[1]), delays)
