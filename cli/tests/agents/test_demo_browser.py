"""Cutting the dead time off the start of a demo video."""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg

from src.agents.demo.browser import _launch_options, trim_start
from src.runtime.browser_mcp import spki_pin
from tests.test_browser_mcp import _ca_pem


def _duration(video: Path) -> float:
    out = subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(video)], capture_output=True, text=True
    ).stderr
    hours, minutes, seconds = out.split("Duration: ", 1)[1].split(",", 1)[0].split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _video(tmp_path: Path, seconds: int) -> Path:
    video = tmp_path / "demo.webm"
    subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=320x240:rate=25",
            "-c:v",
            "libvpx",
            str(video),
        ],
        check=True,
    )
    return video


def test_trim_start_cuts_the_leading_seconds(tmp_path: Path) -> None:
    video = _video(tmp_path, 4)
    trim_start(video, 2.5)
    assert abs(_duration(video) - 1.5) < 0.1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.webm"]


def test_trim_start_leaves_a_negligible_lead_in(tmp_path: Path) -> None:
    video = _video(tmp_path, 2)
    before = video.read_bytes()
    trim_start(video, 0.1)
    assert video.read_bytes() == before


def test_launch_goes_through_the_security_proxy(tmp_path: Path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_bytes(_ca_pem())

    options = _launch_options(
        {"HTTPS_PROXY": "http://127.0.0.1:8080", "NODE_EXTRA_CA_CERTS": str(ca)}
    )

    assert options["proxy"] == {"server": "http://127.0.0.1:8080"}
    assert options["args"] == [f"--ignore-certificate-errors-spki-list={spki_pin(ca.read_bytes())}"]


def test_launch_without_a_proxy_is_a_plain_browser() -> None:
    assert _launch_options({}) == {}
