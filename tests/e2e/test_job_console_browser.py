"""ジョブコンソール（終了通知と入力待ちのダイアログ）を実ブラウザで確かめる E2E."""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import expect

from tests.support.browser import (
    TERMINAL as _TERMINAL,
    LiveServer,
    LiveUi,
    acquire_control as _acquire_control,
    select_led_blinker as _select_led_blinker,
    session_headers as _session_headers,
)
from tests.support.polling import wait_until

_HTTP_TIMEOUT = 10.0


def _current_job(base_url: str) -> dict | None:
    """GET /api/jobs/current の job（無ければ None）を返す."""
    response = httpx.get(f"{base_url}/api/jobs/current", timeout=_HTTP_TIMEOUT)
    assert response.status_code == 200
    return response.json()["job"]


def _start_completion_notice_job(live_ui: LiveUi, browser_page, job_name: str) -> None:
    """実ページのフォームをテスト用ジョブへ向けて開始する。"""
    browser_page.goto(
        f"{live_ui.base_url}/paste_solder",
        wait_until="domcontentloaded",
    )
    _acquire_control(browser_page)
    form = browser_page.get_by_test_id("job-form")
    form.wait_for(state="visible", timeout=10_000)
    form.evaluate(
        """(element, name) => {
            element.dataset.jobName = name;
            element.querySelectorAll('[data-param-type]').forEach((input) => input.remove());
        }""",
        job_name,
    )
    browser_page.get_by_test_id("job-run").click()


class TestCompletionNoticeOverBrowser:
    """実HTTP/WSを経由した終了通知バナーとタイトル。"""

    @pytest.mark.parametrize("width", [1280, 390])
    def test_success_notice_persists_until_dismissed(
        self, live_ui: LiveUi, browser_page, width: int
    ):
        browser_page.set_viewport_size({"width": width, "height": 844})
        original_title = "はんだ塗布 — PCB Assembly WebUI"
        # 通知音は Raspberry Pi 本体で鳴らすので、ブラウザは wav を取得しない
        requested_urls: list[str] = []
        browser_page.on("request", lambda request: requested_urls.append(request.url))
        _start_completion_notice_job(live_ui, browser_page, "completion_notice_success")

        notice = browser_page.get_by_test_id("job-completion-notice")
        notice.wait_for(state="visible", timeout=10_000)
        assert notice.get_attribute("data-status") == "success"
        expect(browser_page.get_by_test_id("job-completion-message")).to_have_text(
            "通知テスト成功が完了しました"
        )
        assert browser_page.title() == f"【成功】{original_title}"
        assert not any(".wav" in url for url in requested_urls)
        # 終了通知が出ても、緊急停止を覆わない。
        assert browser_page.get_by_test_id("estop").evaluate("""el => {
            const box = el.getBoundingClientRect();
            return document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2) === el;
        }""")

        browser_page.get_by_test_id("job-completion-dismiss").click()
        expect(notice).to_be_hidden()
        assert browser_page.title() == original_title

    def test_failure_uses_error_notice_and_title(self, live_ui: LiveUi, browser_page):
        _start_completion_notice_job(live_ui, browser_page, "completion_notice_failure")

        notice = browser_page.get_by_test_id("job-completion-notice")
        notice.wait_for(state="visible", timeout=10_000)
        assert notice.get_attribute("data-status") == "error"
        expect(browser_page.get_by_test_id("job-completion-message")).to_have_text(
            "通知テスト失敗に失敗しました: 通知テスト失敗"
        )
        assert browser_page.title().startswith("【失敗】")

    def test_manual_abort_does_not_notify(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        original_title = "はんだ塗布 — PCB Assembly WebUI"
        _start_completion_notice_job(live_ui, browser_page, "completion_notice_abort")
        wait_until(
            lambda: (job := _current_job(live_server.base_url)) is not None
            and job["status"] == "running",
            timeout=10.0,
            interval=0.05,
        )

        response = httpx.post(
            f"{live_server.base_url}/api/jobs/current/abort", timeout=_HTTP_TIMEOUT
        )
        assert response.status_code == 200
        browser_page.wait_for_function(
            """() => window.webui.jobs.currentJob()?.status === 'aborted'""",
            timeout=10_000,
        )

        expect(browser_page.get_by_test_id("job-completion-notice")).to_be_hidden()
        assert browser_page.title() == original_title

    def test_terminal_job_from_before_page_load_does_not_notify(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        response = httpx.post(
            f"{live_server.base_url}/api/jobs/completion_notice_success",
            timeout=_HTTP_TIMEOUT,
        )
        assert response.status_code == 201
        wait_until(
            lambda: (job := _current_job(live_server.base_url)) is not None
            and job["status"] == "succeeded",
            timeout=10.0,
            interval=0.05,
        )

        browser_page.goto(
            f"{live_ui.base_url}/paste_solder",
            wait_until="domcontentloaded",
        )
        browser_page.wait_for_function(
            """() => window.webui.jobs.currentJob()?.status === 'succeeded'""",
            timeout=10_000,
        )

        expect(browser_page.get_by_test_id("job-completion-notice")).to_be_hidden()
        assert browser_page.title() == "はんだ塗布 — PCB Assembly WebUI"


class TestPromptDialogOverBrowser:
    """実ブラウザ上の prompt modal 表示。"""

    def test_confirm_dialog_uses_custom_button_labels(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        _select_led_blinker(live_server)

        browser_page.goto(
            f"{live_ui.base_url}/height_plane",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
        browser_page.get_by_test_id("job-console").wait_for(
            state="visible", timeout=10_000
        )

        start = httpx.post(
            f"{live_server.base_url}/api/jobs/height_plane",
            headers=_session_headers(browser_page),
            timeout=_HTTP_TIMEOUT,
        )
        assert start.status_code == 201, start.text

        dialog = browser_page.get_by_test_id("jc-prompt")
        dialog.wait_for(state="visible", timeout=30_000)
        ok_button = browser_page.get_by_test_id("jc-prompt-ok")
        no_button = browser_page.get_by_test_id("jc-prompt-no")
        ok_button.wait_for(state="visible", timeout=10_000)
        no_button.wait_for(state="visible", timeout=10_000)

        assert ok_button.inner_text() == "続行"
        assert no_button.inner_text() == "中止"

        no_button.click()
        wait_until(
            lambda: (job := _current_job(live_server.base_url)) is not None
            and job["status"] == "aborted",
            timeout=10.0,
            interval=0.05,
        )

    def test_long_prompt_keeps_the_ok_button_reachable(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """本文が長くても、ダイアログ内スクロールで OK に届く.

        `show()`（非モーダル）で開くので UA の `dialog:modal` の高さクランプが効かない。
        `.jc-prompt[open]` に `max-height` / `overflow` が無いとダイアログが viewport より
        高くなり、`position: fixed` のためページスクロールでも OK に届かず、ダイアログ
        自身もスクロールしないので**応答できないプロンプト**になる。
        """
        _select_led_blinker(live_server)
        browser_page.goto(
            f"{live_ui.base_url}/height_plane",
            wait_until="domcontentloaded",
        )
        browser_page.get_by_test_id("job-console").wait_for(
            state="visible", timeout=10_000
        )

        metrics = browser_page.evaluate(
            """(message) => {
                const byTestId = (testid) =>
                    document.querySelector(`[data-testid="${testid}"]`);
                const dialog = byTestId("jc-prompt");
                byTestId("jc-prompt-message").textContent = message;
                // job_console.js と同じ非モーダル表示（::backdrop で緊急停止を覆わない）
                dialog.show();
                dialog.scrollTop = dialog.scrollHeight;
                const button = byTestId("jc-prompt-ok").getBoundingClientRect();
                return {
                    dialog: dialog.getBoundingClientRect().height,
                    top: button.top,
                    bottom: button.bottom,
                    viewport: window.innerHeight,
                };
            }""",
            "この確認は本文が長いときの表示を見るためのものです。" * 60,
        )

        # ダイアログ自身が viewport に収まる（max-height）
        assert metrics["dialog"] <= metrics["viewport"], metrics
        # スクロールすれば OK が viewport 内に入る（overflow）
        assert metrics["top"] >= 0, metrics
        assert metrics["bottom"] <= metrics["viewport"], metrics

    def test_enter_key_submits_ok_instead_of_cancel(
        self, live_server: LiveServer, live_ui: LiveUi, browser_page
    ):
        """Enter の暗黙送信は OK（続行）に落ちる.

        中止が DOM 先頭の submit ボタンだと Enter が中止を押した扱いになり、
        確認や質量入力のたびにジョブ/サブキャリブが勝手に中止されていた。 続行（True）ならセットアップへ進み、Klipper
        不通（port 7126）で failed になる。旧実装（中止が既定）だと即 aborted になっていた。
        """
        _select_led_blinker(live_server)

        browser_page.goto(
            f"{live_ui.base_url}/height_plane",
            wait_until="domcontentloaded",
        )
        _acquire_control(browser_page)
        browser_page.get_by_test_id("job-console").wait_for(
            state="visible", timeout=10_000
        )

        start = httpx.post(
            f"{live_server.base_url}/api/jobs/height_plane",
            headers=_session_headers(browser_page),
            timeout=_HTTP_TIMEOUT,
        )
        assert start.status_code == 201, start.text

        browser_page.get_by_test_id("jc-prompt").wait_for(
            state="visible", timeout=30_000
        )
        browser_page.keyboard.press("Enter")

        wait_until(
            lambda: (job := _current_job(live_server.base_url)) is not None
            and job["status"] in _TERMINAL,
            timeout=120.0,
            interval=0.1,
        )
        current = _current_job(live_server.base_url)
        assert current is not None
        assert current["status"] == "failed", current
