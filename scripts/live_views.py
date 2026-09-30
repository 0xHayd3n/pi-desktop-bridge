"""Opt-in Pi desktop acceptance test for image views and payload size.

Opens only an owned disposable fixture. Saves images/reports under ignored _local.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import statistics
import struct
import sys
import time
import zlib

from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from pi_desktop_bridge.transport import validate_host

import live_smoke


ROOT = Path(__file__).resolve().parents[1]


def png_rgb(data: bytes) -> tuple[int, int, bytes]:
    """Independent RGB/RGBA8 PNG decoder for the real capture comparisons."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset, compressed = 8, bytearray()
    width = height = channels = 0
    while offset < len(data):
        size = struct.unpack_from("!I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + size]
        assert len(body) == size
        assert zlib.crc32(kind + body) & 0xffffffff == struct.unpack_from("!I", data, offset + 8 + size)[0]
        if kind == b"IHDR":
            width, height, depth, color, compression, filtering, interlace = struct.unpack("!IIBBBBB", body)
            assert depth == 8 and color in (2, 6) and (compression, filtering, interlace) == (0, 0, 0)
            assert 0 < width * height <= 16_777_216
            channels = 3 if color == 2 else 4
        elif kind == b"IDAT":
            compressed.extend(body)
        elif kind == b"IEND":
            break
        offset += size + 12
    expected = height * (width * channels + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, expected + 1)
    assert decoder.eof and len(raw) == expected and not decoder.unused_data
    stride = width * channels
    previous = bytearray(stride)
    rgb = bytearray()
    for y in range(height):
        start = y * (stride + 1)
        filter_type = raw[start]
        row = bytearray(raw[start + 1:start + 1 + stride])
        assert filter_type <= 4
        for index in range(stride):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            diagonal = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                prediction = left
            elif filter_type == 2:
                prediction = above
            elif filter_type == 3:
                prediction = (left + above) // 2
            elif filter_type == 4:
                p = left + above - diagonal
                a, b, c = abs(p - left), abs(p - above), abs(p - diagonal)
                prediction = left if a <= b and a <= c else above if b <= c else diagonal
            else:
                prediction = 0
            row[index] = (row[index] + prediction) & 255
        if channels == 3:
            rgb.extend(row)
        else:
            for index in range(0, stride, 4):
                rgb.extend(row[index:index + 3])
        previous = row
    return width, height, bytes(rgb)


def image_point(metadata: dict, native_x: int, native_y: int) -> dict:
    region = metadata["region"]
    return {
        "x": (native_x - region["x"]) * metadata["width"] // region["width"],
        "y": (native_y - region["y"]) * metadata["height"] // region["height"],
    }


async def exercise(host: str, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report = {"checks": {}, "benchmark": {}}
    fixture = await asyncio.to_thread(live_smoke.start_fixture, host)
    report["fixture_geometry"] = fixture["state"]["geometry"]
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "pi_desktop_bridge", "serve", "--host", host])
    try:
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()

                async def capture(arguments=None, filename=None):
                    start = time.monotonic()
                    result = await session.call_tool("desktop_screenshot", arguments or {})
                    assert not result.isError, result
                    metadata = json.loads(next(item.text for item in result.content if isinstance(item, types.TextContent)))
                    image = next(item for item in result.content if isinstance(item, types.ImageContent))
                    png = base64.b64decode(image.data, validate=True)
                    elapsed = time.monotonic() - start
                    assert struct.unpack("!II", png[16:24]) == (metadata["width"], metadata["height"])
                    assert len(metadata["view_id"]) == 32
                    if filename:
                        (output / filename).write_bytes(png)
                    return metadata, png, elapsed

                # Keep pointer focus in the fixture, outside the compared region.
                # A first click entering from the desktop can be consumed by labwc.
                canvas = fixture["state"]["geometry"]["canvas"]
                moved = await session.call_tool("desktop_move", {
                    "x": canvas["x"] + canvas["width"] - 5,
                    "y": canvas["y"] + canvas["height"] - 5,
                })
                assert not moved.isError, moved
                # Use the same compositor RGB format as a crop for a strict
                # cross-capture comparison. Native PNG and PPM captures can
                # differ slightly at rendered edges.
                full, full_png, _ = await capture({"max_width": 65535}, "full-reference.png")
                # Stay below text antialiasing, which can vary by one RGB unit
                # between separately rendered frames. Include solid-color tiles.
                region = {"x": canvas["x"] + 20, "y": canvas["y"] + 50,
                          "width": canvas["width"] - 40, "height": canvas["height"] - 70}
                detail, detail_png, _ = await capture(region, "detail.png")
                assert detail["region"] == region and (detail["width"], detail["height"]) == (region["width"], region["height"])
                dw, dh, desktop_pixels = await asyncio.to_thread(png_rgb, full_png)
                rw, rh, detail_pixels = await asyncio.to_thread(png_rgb, detail_png)
                expected = b"".join(desktop_pixels[((region["y"] + row) * dw + region["x"]) * 3:
                                                  ((region["y"] + row) * dw + region["x"] + rw) * 3]
                                    for row in range(rh))
                assert detail_pixels == expected, "Native crop did not preserve exact RGB pixels"
                for index, color in enumerate((b"\xff\0\0", b"\0\xff\0", b"\0\0\xff", b"\xff\xff\xff")):
                    offset = (25 * rw + 3 + index * 10) * 3
                    assert detail_pixels[offset:offset + 3] == color
                report["checks"]["native_crop_exact_rgb"] = True

                overview, _, _ = await capture({"max_width": 960}, "overview.png")
                assert overview["width"] == min(dw, 960)
                assert (overview["desktop_width"], overview["desktop_height"]) == (dw, dh)
                button = fixture["state"]["geometry"]["button"]
                point = image_point(overview, button["x"] + button["width"] // 2,
                                    button["y"] + button["height"] // 2)
                clicked = await session.call_tool("desktop_click", {**point, "view_id": overview["view_id"]})
                assert not clicked.isError, clicked
                report["mapped_click"] = {"view": overview, "point": point}
                clicked_image = next(item for item in clicked.content if isinstance(item, types.ImageContent))
                (output / "after-click.png").write_bytes(base64.b64decode(clicked_image.data, validate=True))
                state = await asyncio.to_thread(live_smoke.wait_for_state, host, fixture, {"clicks": 1})
                report["mapped_click"]["receipt"] = state
                assert state["clicks"] == 1, state
                report["checks"]["overview_mapped_click"] = True
                stale = await session.call_tool("desktop_click", {**point, "view_id": overview["view_id"]})
                assert stale.isError, stale
                state = await asyncio.to_thread(live_smoke.wait_for_state, host, fixture, {"clicks": 1})
                assert state["clicks"] == 1
                report["checks"]["stale_view_rejected"] = True

                view, _, _ = await capture(region)
                start_native = (canvas["x"] + 60, canvas["y"] + 65)
                end_native = (canvas["x"] + 400, canvas["y"] + 115)
                start, end = image_point(view, *start_native), image_point(view, *end_native)
                dragged = await session.call_tool("desktop_drag", {
                    "start_x": start["x"], "start_y": start["y"],
                    "end_x": end["x"], "end_y": end["y"], "view_id": view["view_id"],
                })
                assert not dragged.isError, dragged
                state = await asyncio.to_thread(live_smoke.wait_for_state, host, fixture,
                                                {"drag": [[60, 65], [400, 115]]})
                assert state["drag"] == [[60, 65], [400, 115]], state
                report["checks"]["crop_mapped_drag"] = True

                view, _, _ = await capture({**region, "max_width": 280})
                target = image_point(view, canvas["x"] + 250, canvas["y"] + 80)
                scrolled = await session.call_tool("desktop_scroll", {
                    **target, "direction": "down", "ticks": 2, "view_id": view["view_id"],
                })
                assert not scrolled.isError, scrolled
                state = await asyncio.to_thread(live_smoke.wait_for_state, host, fixture, {"scrolls": 2})
                assert state["scrolls"] == 2
                report["checks"]["resized_crop_mapped_scroll"] = True

                for name, arguments in (("full", {}), ("overview", {"max_width": 960}), ("detail", region)):
                    samples = []
                    for _ in range(3):
                        metadata, png, elapsed = await capture(arguments)
                        samples.append({"seconds": round(elapsed, 4), "png_bytes": len(png),
                                        "width": metadata["width"], "height": metadata["height"]})
                    report["benchmark"][name] = {
                        "samples": samples, "median_seconds": round(statistics.median(x["seconds"] for x in samples), 4),
                        "median_png_bytes": statistics.median(x["png_bytes"] for x in samples),
                    }
                baseline = report["benchmark"]["full"]["median_png_bytes"]
                overview_size = report["benchmark"]["overview"]["median_png_bytes"]
                assert overview_size < baseline, report["benchmark"]
                report["benchmark"]["overview_payload_reduction_percent"] = round(100 * (1 - overview_size / baseline), 1)
                report["checks"]["overview_smaller_payload"] = True

                token = metadata["view_id"]
                assert not (await session.call_tool("desktop_disconnect", {})).isError
                old = await session.call_tool("desktop_move", {"x": 0, "y": 0, "view_id": token})
                assert old.isError
                health = await session.call_tool("desktop_health", {})
                health_json = json.loads(next(item.text for item in health.content if isinstance(item, types.TextContent)))
                assert health_json["session_active"] is False
                report["checks"]["disconnect_invalidates_view_without_acquiring"] = True
    finally:
        report["checks"].update(await asyncio.to_thread(live_smoke.stop_fixture, host, fixture))
        (output / "live-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "_local/live-views")
    args = parser.parse_args()
    validate_host(args.host)
    print(json.dumps(asyncio.run(exercise(args.host, args.output.resolve())), indent=2))


if __name__ == "__main__":
    main()
