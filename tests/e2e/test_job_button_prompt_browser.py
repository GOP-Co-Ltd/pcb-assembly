"""ボタンで選ぶ prompt（kind="buttons"）: 選択肢のボタンと、別段の副ボタン。"""

from collections.abc import Iterator

import pytest
from playwright.sync_api import expect

from pcbasm.hal.fake import FakeAudioPlayer
from tests.support.browser import (
    LiveServer,
    LiveUi,
    acquire_control,
    configure_synthetic_job,
    start_app,
)
from tests.support.web import devices_with_audio
from web.api.app import create_app as create_api_app
from web.api.env import Env
from web.api.jobs.runtime.context import JobContext
from web.api.jobs.runtime.record import JobResult


@pytest.fixture
def live_server(e2e_settings: Env) -> Iterator[LiveServer]:
    app = create_api_app(
        e2e_settings, devices=devices_with_audio(e2e_settings, FakeAudioPlayer())
    )

    def run(ctx: JobContext) -> JobResult:
        answer = ctx.choose_button(
            "どちらのサーボですか?",
            ("Rotation", "Open Close"),
            default="Open Close",
            other="もう一度動かす",
        )
        return JobResult(summary=f"accepted:{answer}")

    configure_synthetic_job(app, "camera_calibration", run)
    server = start_app(app)
    try:
        yield LiveServer(
            base_url=f"http://127.0.0.1:{server.port}",
            settings=e2e_settings,
            port=server.port,
        )
    finally:
        server.stop()


def _open_prompt(page, ui: LiveUi) -> None:
    page.goto(f"{ui.base_url}/camera_calibration")
    acquire_control(page)
    page.get_by_test_id("job-run").click()
    expect(page.get_by_test_id("jc-prompt")).to_be_visible()


class TestButtonPrompt:
    def test_choices_are_buttons_and_the_other_is_apart(
        self, live_ui: LiveUi, browser_page
    ):
        page = browser_page
        _open_prompt(page, live_ui)

        field = page.get_by_test_id("jc-prompt-field")
        expect(field.get_by_role("button")).to_have_text(["Rotation", "Open Close"])
        expect(field.locator("select")).to_have_count(0)
        expect(page.get_by_test_id("jc-prompt-ok")).to_be_hidden()
        # 副ボタンは選択肢の段ではなく、下の段に置く
        expect(page.get_by_test_id("jc-prompt-no")).to_have_text("もう一度動かす")
        expect(field.get_by_text("もう一度動かす")).to_have_count(0)

    def test_default_button_has_the_focus(self, live_ui: LiveUi, browser_page):
        page = browser_page
        _open_prompt(page, live_ui)

        expect(
            page.get_by_test_id("jc-prompt-field").get_by_role(
                "button", name="Open Close"
            )
        ).to_be_focused()

    @pytest.mark.parametrize("label", ["Rotation", "Open Close"])
    def test_pressing_a_choice_answers_it(
        self, live_ui: LiveUi, browser_page, label: str
    ):
        page = browser_page
        _open_prompt(page, live_ui)
        page.get_by_test_id("jc-prompt-field").get_by_role("button", name=label).click()

        expect(page.get_by_test_id("jc-summary")).to_have_text(f"accepted:{label}")
        expect(page.get_by_test_id("jc-prompt")).to_be_hidden()

    def test_the_other_button_answers_its_label(self, live_ui: LiveUi, browser_page):
        page = browser_page
        _open_prompt(page, live_ui)
        page.get_by_test_id("jc-prompt-no").click()

        expect(page.get_by_test_id("jc-summary")).to_have_text(
            "accepted:もう一度動かす"
        )
