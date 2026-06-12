"""`webui.routers.files` の仕様テスト.

計画書「routers」節:

- GET /api/files — dir 全部 + *.kicad_pcb のみ、名前順。traversal は 400、不存在は 404
- PUT /api/pcb-file — StateResponse 返却。拡張子・root 外は 400、busy は 409
"""

from fastapi.testclient import TestClient

from webui.state import AppState


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
