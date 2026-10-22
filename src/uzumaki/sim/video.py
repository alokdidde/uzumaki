"""Offscreen video of a simulated case, written as MP4 through ``ffmpeg``.

Rendering needs an OpenGL backend: set ``MUJOCO_GL=egl`` (GPU) or ``MUJOCO_GL=osmesa`` (software,
slower) on headless machines. Frames are sampled on simulation time, so the video plays in real
time regardless of how fast the physics ran.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import mujoco

from uzumaki.sim import SimUnavailable


class VideoRecorder:
    """Frame hook for ``SimWorld``: samples the scene at ``fps`` from a fixed overview camera."""

    def __init__(self, fps: int = 20, width: int = 640, height: int = 480) -> None:
        self.fps = fps
        self.width = width
        self.height = height
        self.frames: list[bytes] = []
        self._renderer: Any = None
        self._camera: Any = None
        self._next_time = 0.0

    def __call__(self, model: Any, data: Any) -> None:
        if self._renderer is None:
            self._renderer = mujoco.Renderer(model, self.height, self.width)
            camera = mujoco.MjvCamera()
            camera.lookat[:] = [0.0, -0.05, 0.09]
            camera.distance = 0.85
            camera.azimuth = -90.0
            camera.elevation = -32.0
            self._camera = camera
            self._next_time = data.time
        if data.time + 1e-9 < self._next_time:
            return
        self._next_time += 1.0 / self.fps
        self._renderer.update_scene(data, self._camera)
        self.frames.append(self._renderer.render().tobytes())

    def write(self, path: Path) -> Path:
        """Encode the captured frames to ``path`` (H.264, yuv420p)."""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SimUnavailable("ffmpeg is required to write videos")
        command = [
            ffmpeg, "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps), "-i", "-",
            "-pix_fmt", "yuv420p", "-vcodec", "libx264", str(path),
        ]  # fmt: skip
        subprocess.run(command, input=b"".join(self.frames), check=True)
        return path

    def close(self) -> None:
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None
