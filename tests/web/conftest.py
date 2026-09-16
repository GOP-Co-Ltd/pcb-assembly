"""Backend API と frontend SSR で共用する、破損・未完成のマシン設定。

各テスト階層の ``config_dir`` が指す一時コピーだけを変更する。
同じ入力素材を使い、API 応答と SSR の回帰をそれぞれの境界で検証する。
"""

from pathlib import Path

import pytest


@pytest.fixture
def partial_nozzle_cap(config_dir: Path) -> Path:
    """`[paste_dispenser.nozzle_cap]` に x だけを書いた machine.toml を用意する（そのパスを返す）.

    設定画面から 1 軸だけ保存すると実際にこの配置になり、`Machine.nozzle_cap` の
    `Machine` は「未記録」として扱う。`/api/state` と全ページ SSR がこれで 500 しないことを
    ピンするための素材（`AppState.nozzle_cap()` の防御が要）。
    """
    path = config_dir / "machine.toml"
    with path.open("a", encoding="utf-8") as machine_toml:
        machine_toml.write("\n[paste_dispenser.nozzle_cap]\nx = 12.5\n")
    return path


@pytest.fixture
def partial_nozzle_clean(config_dir: Path) -> Path:
    """`[paste_dispenser.nozzle_clean]` に動作値だけを書いた machine.toml を用意する.

    設定画面から押し込み量だけ保存すると座標の無いテーブルができる。`NozzleClean` は
    座標必須なので「未記録」として扱われる（既定値で埋めると原点へ移動する事故になる）。
    """
    path = config_dir / "machine.toml"
    with path.open("a", encoding="utf-8") as machine_toml:
        machine_toml.write("\n[paste_dispenser.nozzle_clean]\npress_depth = 0.4\n")
    return path


@pytest.fixture
def broken_machine_toml(config_dir: Path) -> Path:
    """終端されていない文字列を追記して machine.toml をパース不能にする（そのパスを返す）.

    `Machine()` も `tomlkit` もこの machine.toml で例外を投げる。全ページの SSR が
    共通で呼ぶ `AppState.machine_name()` / `machine_type()` / `focus_z()` の防御
    （broad except → None）が効いていることをピンするための素材。
    """
    path = config_dir / "machine.toml"
    with path.open("a", encoding="utf-8") as machine_toml:
        machine_toml.write('\nbroken_key = "unterminated\n')
    return path
