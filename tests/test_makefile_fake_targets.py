"""`make api-fake` / `make ui-fake` の隔離契約テスト.

fake 起動は camera と data_dir だけが fake で、``config_dir`` は実機のものを読む。
mDNS を切らずに起動すると**実機と同じ machine_id が実 LAN に広告される**（frontend の
マージは同一 machine_id なら先に発見した方を残すため、ドロップダウンの実機エントリが
fake backend の port を指しうる）。この性質を守っているのは Makefile のレシピにある
env 1 行だけなので、レシピを読んでピンする。

`tests/test_claude_hooks.py` と同じくミラーレイアウト外のトップレベルテスト
（`src/` に対応物が無い成果物のため）。
"""

from __future__ import annotations

import itertools
import re
import subprocess

import pytest

from pcbasm.utils import PROJECT_ROOT

MAKEFILE = PROJECT_ROOT / "Makefile"


def recipe(target: str) -> str:
    """Makefile から 1 ターゲットのレシピ行（タブ始まりの行）を取り出す.

    Args:
        target: ターゲット名（``api-fake`` など）

    Returns:
        レシピ行を連結した文字列

    Raises:
        AssertionError: ターゲットが無い、またはレシピが空のとき
    """
    lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(f"{target}:")]
    assert starts, f"Makefile に {target} ターゲットが無い"
    body = list(
        itertools.takewhile(lambda line: line.startswith("\t"), lines[starts[0] + 1 :])
    )
    assert body, f"{target} のレシピが空"
    return "\n".join(body)


class TestFakeTargetsDisableDiscovery:
    """Fake 起動が実機の machine_id を実 LAN に広告・探索しない."""

    @pytest.mark.parametrize(
        ("target", "kill_switch"),
        (
            ("api-fake", "PCBASM_API_DISCOVERY_ENABLED=0"),
            ("ui-fake", "PCBASM_UI_DISCOVERY_ENABLED=0"),
        ),
    )
    def test_recipe_sets_the_kill_switch(self, target: str, kill_switch: str):
        assert kill_switch in recipe(target)


class TestWebuiAliasesAreGone:
    """`webui` 系エイリアスが残っていない.

    `scripts/web-service.sh` が生成する unit は `make api` / `make ui`
    を実行する。エイリアスを 復活させると旧 `pcbasm-webui.service`（`ExecStart=make
    webui`）が動いてしまい、 2 プロセス構成へ移行しきれない。
    """

    def test_makefile_defines_no_webui_target(self):
        lines = MAKEFILE.read_text(encoding="utf-8").splitlines()
        webui_targets = [line for line in lines if re.match(r"^webui[a-z-]*\s*:", line)]
        assert not webui_targets, webui_targets

    @pytest.mark.parametrize(
        "target", ("api", "api-dev", "api-fake", "ui", "ui-dev", "ui-fake")
    )
    def test_final_targets_exist(self, target: str):
        assert recipe(target)


class TestHelpListsEveryDocumentedTarget:
    """`make help` が `## ` コメント付きの全ターゲットを出す.

    help は grep の文字クラスでターゲット名を拾うため、名前に含まれる文字種を落とすと 黙って一覧から消える（数字を含む
    `test-e2e` が README で案内されているのに出ない、 という不整合が起きていた）。`make help` は grep
    + awk だけで副作用が無いので実行して 出力を確認する。
    """

    @pytest.mark.parametrize("target", ("test-e2e", "test-no-hardware", "api", "ui"))
    def test_help_lists(self, target: str):
        completed = subprocess.run(
            ["make", "-f", str(MAKEFILE), "help"],
            cwd=MAKEFILE.parent,
            capture_output=True,
            text=True,
            check=True,
        )
        listed = re.findall(r"\x1b\[36m(\S+)", completed.stdout)
        assert target in listed, completed.stdout
