"""テストの待ち合わせ（sleep 固定値に頼らないポーリングと締め切り）."""

import threading
import time
from collections.abc import Callable

import pytest


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.02,
) -> None:
    """条件が成立するまでポーリングする（タイミングのアサートはしない）.

    成立しないまま timeout を超えたら pytest.fail する。sleep 固定値依存のアサートを避けるための共有ポーラ。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    pytest.fail(f"{timeout}s 以内に条件が成立しませんでした")


def before_deadline[T](
    call: Callable[[], T], *, what: str = "応答", deadline: float = 15.0
) -> T:
    """`deadline` 以内に返らなければ失敗させる（ハングをテスト失敗に変える）.

    ブロッキングする待ち（`TestClient` の WS receive / `httpx` のストリーム読み）は
    `pytest.mark.timeout` では中断できず、テストが「落ちる」のではなく「終わらない」。
    締め切りをテスト側に持たせるための共有ヘルパ（daemon スレッドで走らせて join する）。
    """
    outcome: list[T] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # noqa: BLE001 - 呼び出し元へそのまま送り直す
            failures.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline)
    if failures:
        raise failures[0]
    if not outcome:
        pytest.fail(f"{deadline}s 以内に{what}が返りませんでした")
    return outcome[0]
