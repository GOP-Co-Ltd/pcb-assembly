"""`webui.routers.files` の仕様テスト.

計画書「routers」節:

- GET /api/files — dir 全部 + *.kicad_pcb のみ、名前順。traversal は 400、不存在は 404
- PUT /api/pcb-file — StateResponse 返却。拡張子・root 外は 400、busy は 409

MR2（計画書 docs/plans/web-api-ui-split.md「MR2」節「ファイル公開範囲を絞る」）が
追記契約:

- `pcb_browse_root` は `board_id` の算出基準なので `/` のまま変えない。代わりに
  `Settings.pcb_browse_allowed`（既定 = リポジトリルート + /media + /mnt）の
  サブツリーだけを公開する
- 許可サブツリー外は列挙も選択も 400。許可サブツリーと文字列 prefix を共有する兄弟
  （`.../pcb` に対する `.../pcb-evil`）も外側として扱う
- 許可サブツリーへ辿るための祖先ディレクトリは列挙に出す（`/` から `/media` の USB
  を選べるようにするため）。ただし中身は見せない
- アップロード先も許可サブツリー内であることを検証する
"""

from pathlib import Path

import attrs
import pytest
from fastapi.testclient import TestClient

from webui.app import create_app
from webui.settings import Settings
from webui.state import AppState


def _rel_to_slash(path: Path) -> str:
    """pcb_browse_root="/" から見た相対 posix パス."""
    return path.resolve().relative_to(Path("/")).as_posix()


def _entries(client: TestClient, path: str) -> set[tuple[str, str]]:
    response = client.get("/api/files", params={"path": path})
    assert response.status_code == 200
    return {(entry["name"], entry["type"]) for entry in response.json()["entries"]}


@pytest.fixture
def slash_root_client(webui_settings: Settings, pcb_root: Path):
    """実機の既定形（pcb_browse_root="/" + 許可サブツリーのみ公開）の TestClient."""
    settings = attrs.evolve(
        webui_settings, pcb_browse_root=Path("/"), pcb_browse_allowed=(pcb_root,)
    )
    with TestClient(create_app(settings)) as client:
        yield client


class TestFilesApi:
    """GET /api/files."""

    def test_root_listing_contains_dirs_and_kicad_pcb_only(self, client: TestClient):
        response = client.get("/api/files", params={"path": ""})

        assert response.status_code == 200
        data = response.json()
        assert data["path"] == ""
        entries = {(entry["name"], entry["type"]) for entry in data["entries"]}
        assert entries == {
            ("boards", "dir"),
            ("docs", "dir"),
            ("top.kicad_pcb", "file"),
        }
        names = [entry["name"] for entry in data["entries"]]
        assert names == sorted(names)

    def test_subdirectory_listing_excludes_other_extensions(self, client: TestClient):
        response = client.get("/api/files", params={"path": "boards"})

        assert response.status_code == 200
        data = response.json()
        assert data["path"] == "boards"
        entries = {(entry["name"], entry["type"]) for entry in data["entries"]}
        assert entries == {("sample.kicad_pcb", "file")}

    def test_traversal_path_returns_400(self, client: TestClient):
        response = client.get("/api/files", params={"path": "../.."})

        assert response.status_code == 400

    def test_missing_path_returns_404(self, client: TestClient):
        response = client.get("/api/files", params={"path": "no-such-dir"})

        assert response.status_code == 404


class TestPcbFileApi:
    """PUT /api/pcb-file."""

    def test_put_pcb_file_updates_state(self, client: TestClient):
        response = client.put("/api/pcb-file", json={"path": "boards/sample.kicad_pcb"})

        assert response.status_code == 200
        assert response.json()["pcb_file"] == "boards/sample.kicad_pcb"
        assert client.get("/api/state").json()["pcb_file"] == "boards/sample.kicad_pcb"

    def test_put_non_kicad_pcb_returns_400(self, client: TestClient):
        response = client.put("/api/pcb-file", json={"path": "boards/notes.txt"})

        assert response.status_code == 400

    def test_put_path_outside_root_returns_400(self, client: TestClient):
        response = client.put("/api/pcb-file", json={"path": "../outside.kicad_pcb"})

        assert response.status_code == 400

    def test_put_missing_file_returns_404(self, client: TestClient):
        response = client.put("/api/pcb-file", json={"path": "boards/ghost.kicad_pcb"})

        assert response.status_code == 404

    def test_put_while_busy_returns_409(self, client: TestClient, appstate: AppState):
        with appstate.machine_lock("pytest-job"):
            response = client.put(
                "/api/pcb-file", json={"path": "boards/sample.kicad_pcb"}
            )

        assert response.status_code == 409

    def test_browse_root_slash_allows_any_allowed_subtree_path(
        self, slash_root_client: TestClient, pcb_root: Path
    ):
        """pcb_browse_root="/" のまま、許可サブツリー（USB マウント等）を選択できる.

        root を "/" から動かさないのは `board_id` の算出基準だから（board_settings）。
        公開範囲は `pcb_browse_allowed` で絞る。
        """
        rel = _rel_to_slash(pcb_root / "top.kicad_pcb")

        response = slash_root_client.put("/api/pcb-file", json={"path": rel})

        assert response.status_code == 200
        assert response.json()["pcb_file"] == rel


class TestPcbBrowseAllowed:
    """公開範囲（`Settings.pcb_browse_allowed`）の境界（MR2）."""

    def test_root_listing_shows_only_the_way_to_allowed_subtrees(
        self, slash_root_client: TestClient, pcb_root: Path
    ):
        """ルート直下の列挙は許可サブツリーへ続くディレクトリだけを出す.

        USB を選ぶには `/media` へ辿れる必要があるので祖先は見せるが、それ以外の
        トップレベル（/etc, /home ...）は出さない。
        """
        first_component = pcb_root.resolve().parts[1]

        assert _entries(slash_root_client, "") == {(first_component, "dir")}

    def test_ancestor_listing_hides_entries_outside_allowed_subtree(
        self, slash_root_client: TestClient, pcb_root: Path
    ):
        """祖先ディレクトリの列挙では、許可サブツリーへ続く道以外を見せない.

        prefix 兄弟（`.../pcb-evil`）もここに出てはいけないので、等値で assert する。
        """
        parent = pcb_root.resolve().parent
        assert (parent / "outside.kicad_pcb").is_file()
        assert (parent / f"{pcb_root.name}-evil").is_dir()

        assert _entries(slash_root_client, _rel_to_slash(parent)) == {("pcb", "dir")}

    def test_allowed_subtree_lists_full_contents(
        self, slash_root_client: TestClient, pcb_root: Path
    ):
        """許可サブツリーの内側は従来どおり dir 全部 + *.kicad_pcb を出す."""
        assert _entries(slash_root_client, _rel_to_slash(pcb_root)) == {
            ("boards", "dir"),
            ("docs", "dir"),
            ("top.kicad_pcb", "file"),
        }

    def test_listing_directory_outside_allowed_subtree_returns_400(
        self, slash_root_client: TestClient
    ):
        """許可サブツリー外の実ディレクトリ（root 配下）は列挙できない."""
        response = slash_root_client.get("/api/files", params={"path": "etc"})

        assert response.status_code == 400

    def test_selecting_file_outside_allowed_subtree_returns_400(
        self, slash_root_client: TestClient, pcb_root: Path
    ):
        """許可サブツリー外の .kicad_pcb は root 配下でも選択できない."""
        outside = pcb_root.resolve().parent / "outside.kicad_pcb"

        response = slash_root_client.put(
            "/api/pcb-file", json={"path": _rel_to_slash(outside)}
        )

        assert response.status_code == 400

    def test_listing_prefix_sibling_of_allowed_subtree_returns_400(
        self, slash_root_client: TestClient, pcb_root: Path
    ):
        """許可サブツリーと文字列 prefix を共有する兄弟ディレクトリは列挙できない.

        `.../pcb` を許可したときに `.../pcb-evil` が読めてしまうのが、許可判定を
        前方一致（`startswith`）で書いた場合の典型的な脱出経路。
        """
        sibling = pcb_root.resolve().parent / f"{pcb_root.name}-evil"
        assert (sibling / "secret.kicad_pcb").is_file()

        response = slash_root_client.get(
            "/api/files", params={"path": _rel_to_slash(sibling)}
        )

        assert response.status_code == 400

    def test_selecting_file_in_prefix_sibling_returns_400(
        self, slash_root_client: TestClient, pcb_root: Path
    ):
        """Prefix 兄弟の中の .kicad_pcb は選択もできない."""
        secret = (
            pcb_root.resolve().parent / f"{pcb_root.name}-evil" / "secret.kicad_pcb"
        )

        response = slash_root_client.put(
            "/api/pcb-file", json={"path": _rel_to_slash(secret)}
        )

        assert response.status_code == 400

    def test_upload_destination_outside_allowed_subtree_returns_400(
        self, webui_settings: Settings, tmp_path: Path
    ):
        """アップロード先が許可サブツリー外なら保存前に 400 で断る."""
        settings = attrs.evolve(webui_settings, pcb_upload_dir=tmp_path / "elsewhere")
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/api/pcb-file/upload",
                files={"file": ("board.kicad_pcb", b"(kicad_pcb)")},
            )

        assert response.status_code == 400
        assert not (tmp_path / "elsewhere").exists()


class TestPcbUploadApi:
    """POST /api/pcb-file/upload — uploads/ へ保存しそのまま選択する."""

    def test_upload_saves_and_selects(self, client: TestClient, pcb_root):
        response = client.post(
            "/api/pcb-file/upload",
            files={"file": ("board.kicad_pcb", b"(kicad_pcb)")},
        )

        assert response.status_code == 201
        assert response.json()["pcb_file"] == "uploads/board.kicad_pcb"
        saved = pcb_root / "uploads" / "board.kicad_pcb"
        assert saved.read_bytes() == b"(kicad_pcb)"
        # アップロード先はファイルブラウザからも見える
        listing = client.get("/api/files", params={"path": "uploads"}).json()
        assert {(e["name"], e["type"]) for e in listing["entries"]} == {
            ("board.kicad_pcb", "file")
        }

    def test_upload_strips_directory_components(self, client: TestClient, pcb_root):
        response = client.post(
            "/api/pcb-file/upload",
            files={"file": ("../evil.kicad_pcb", b"(kicad_pcb)")},
        )

        assert response.status_code == 201
        assert response.json()["pcb_file"] == "uploads/evil.kicad_pcb"
        assert (pcb_root / "uploads" / "evil.kicad_pcb").is_file()
        assert not (pcb_root.parent / "evil.kicad_pcb").exists()

    def test_upload_non_kicad_pcb_returns_400(self, client: TestClient):
        response = client.post(
            "/api/pcb-file/upload", files={"file": ("notes.txt", b"not a pcb")}
        )

        assert response.status_code == 400

    def test_upload_while_busy_returns_409(
        self, client: TestClient, appstate: AppState
    ):
        with appstate.machine_lock("pytest-job"):
            response = client.post(
                "/api/pcb-file/upload",
                files={"file": ("board.kicad_pcb", b"(kicad_pcb)")},
            )

        assert response.status_code == 409
