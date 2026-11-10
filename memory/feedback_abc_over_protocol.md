---
name: 差し替え口は Protocol ではなく ABC
description: 実装を差し替える口（HAL・手順の受け口など）は typing.Protocol ではなく abc.ABC で書き、実装は継承して @override を付ける
type: feedback
---

差し替え口（インターフェース）は `typing.Protocol` ではなく `abc.ABC` + `@abstractmethod` で書く。実装クラスは基底を継承し、各メソッドに `typing.override` を付ける。

**Why:** ユーザーの方針（2026-11-10）。このリポジトリの実装はどれも自前のクラスで、構造的部分型（継承なしで形が合えば通す）の利点が無い。ABC なら実装が何を満たすかを明示でき、抽象メソッドの書き忘れはインスタンス化で、取り違えは `@override` で気づける。Claude は習慣で Protocol を選びがちだったので、意識して避ける。

**How to apply:**

- 新しい差し替え口は ABC で書く（例: `pcbasm.hal.audio.AudioPlayer`、`pcbasm.hal.klipper.KlipperClient`）
- fake も本番と同じ基底を継承する（`FakeKlipper(KlipperClient)` など）
- 抽象基底を作らずに済むなら作らない方針（具象クラス 1 つで足りる場合）は変わらない
