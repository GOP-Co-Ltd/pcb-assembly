"""Small real-PNG paste-volume datasets used by ML integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import torch
from torchvision.io import write_png


def write_synthetic_session(
    root: Path,
    session_id: str,
    *,
    machine_id: str = "machine-a",
    paste_id: str = "paste-a",
    paste_lot: str | None = "lot-a",
    nozzle_diameter_mm: float = 0.3,
    pad_count: int = 2,
    views_per_pad: int = 1,
    width: int = 64,
    height: int = 48,
    pixel_per_mm: float = 20.0,
    content_seed: int | None = None,
) -> Path:
    session = root / session_id
    for directory in ("pre", "post", "mask"):
        (session / directory).mkdir(parents=True, exist_ok=False)
    seed = content_seed if content_seed is not None else sum(session_id.encode("utf-8"))
    pads: list[dict[str, object]] = []
    total_volume = 0.05
    total_rotations = 0.5
    modes = ("dot", "line", "area")
    for pad_index in range(1, pad_count + 1):
        measured = 0.1 + pad_index * 0.01
        rotations = measured * 10
        views: list[dict[str, object]] = []
        for view_number in range(views_per_pad):
            filename = f"{pad_index:06d}.{view_number:02d}.png"
            row = torch.arange(height, dtype=torch.int16).reshape(height, 1)
            column = torch.arange(width, dtype=torch.int16).reshape(1, width)
            offset = (seed + pad_index * 17 + view_number * 31) % 151
            red = (row + column + offset).remainder(256).to(torch.uint8)
            green = (row * 2 + column + offset + 29).remainder(256).to(torch.uint8)
            blue = (row + column * 2 + offset + 71).remainder(256).to(torch.uint8)
            pre = torch.stack((red, green, blue))
            post = pre.clone()
            post[:, height // 4 : height // 2, width // 4 : width // 2] = (
                (
                    post[:, height // 4 : height // 2, width // 4 : width // 2].to(
                        torch.int16
                    )
                    + 23
                )
                .remainder(256)
                .to(torch.uint8)
            )
            mask = torch.zeros((1, height, width), dtype=torch.uint8)
            mask[:, 4 : height - 4, 4 : width - 4] = 255
            write_png(pre, str(session / "pre" / filename))
            write_png(post, str(session / "post" / filename))
            write_png(mask, str(session / "mask" / filename))
            views.append(
                {
                    "number": view_number,
                    "offset_x_mm": 0.0,
                    "offset_y_mm": 0.0,
                    "pixel_rect": [10, 20, 10 + width, 20 + height],
                    "pre": f"pre/{filename}",
                    "post": f"post/{filename}",
                    "mask": f"mask/{filename}",
                }
            )
        mode = modes[(pad_index - 1) % len(modes)]
        pads.append(
            {
                "index": pad_index,
                "pad_id": f"P{pad_index}.1",
                "source_pad_id": f"P{pad_index}.1",
                "polygon": {
                    "exterior": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]],
                    "holes": [],
                },
                "resolved": {
                    "dispense_mode": mode,
                    "line_direction": "outward",
                    "paste_height": "auto",
                    "ul_per_mm2": 0.1,
                    "prime_extra_delay": 0.0,
                    "bead_width_factor": 1.0,
                    "overlap": 0.2,
                    "boundary_margin": 0.0,
                },
                "execution": {
                    "applied_mode": mode,
                    "path_length_mm": float(pad_index),
                    "commanded_volume_ul": measured,
                    "prime_extra_volume_ul": 0.0,
                    "effective_rate_ul_s": 0.05,
                    "rotations": rotations,
                },
                "measured_volume_ul": measured,
                "views": views,
            }
        )
        total_volume += measured
        total_rotations += rotations
    metadata = {
        "kind": "pcbasm-paste-volume-dataset",
        "schema_version": 1,
        "created_at": "2026-09-01T00:00:00+00:00",
        "machine": {"machine_id": machine_id, "name": machine_id},
        "board": {
            "filename": "fixture.kicad_pcb",
            "source_pcb": "fixture.kicad_pcb",
            "signature": f"board-{seed}",
        },
        "paste": {
            "paste_id": paste_id,
            "lot": paste_lot,
            "density_mg_per_ul": 4.0,
        },
        "camera": {
            "pixel_per_mm": pixel_per_mm,
            "resolution": [1920, 1080],
            "calibrated_at": "2026-09-01T00:00:00+00:00",
            "z_position_mm": 10.0,
        },
        "nozzle": {"diameter_mm": nozzle_diameter_mm},
        "config": {
            "rotations_per_ul": 10.0,
            "max_fill_speed_mm_s": 1.0,
            "max_dispense_rate_ul_s": 0.1,
            "dispense_accel_ul_s2": 0.2,
            "retract_amount_ul": 0.01,
            "retract_rate_ul_s": 0.1,
            "initial_purge_ul": 0.05,
            "crop_margin_mm": 1.0,
            "mask_margin_mm": 0.5,
        },
        "total": {
            "measured_mass_mg": total_volume * 4,
            "measured_volume_ul": total_volume,
            "rotations": total_rotations,
        },
        "purge": {
            "pad_id": "PURGE.1",
            "source_pad_id": "PURGE.1",
            "execution": {
                "applied_mode": "dot",
                "path_length_mm": 0,
                "commanded_volume_ul": 0.05,
                "prime_extra_volume_ul": 0.0,
                "effective_rate_ul_s": 0.05,
                "rotations": 0.5,
            },
            "measured_volume_ul": 0.05,
        },
        "pads": pads,
    }
    (session / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return session


def read_metadata(session: Path) -> dict[str, object]:
    return json.loads((session / "metadata.json").read_text(encoding="utf-8"))


def write_metadata(session: Path, metadata: dict[str, object]) -> None:
    (session / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
