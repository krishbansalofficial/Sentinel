"""Record the README demo: the terminal UI against a real backend holding real backtest runs.

Nothing is mocked. A disposable backend (temporary store) records three of the committed
backtest results (bench/regression_detection/results), a Change is created on a throwaway
repository, and the real TUI is driven with Textual's pilot. Each frame is Textual's own SVG
screenshot; Chromium rasterizes them and Pillow writes the GIF.

    pip install pillow   # demo-only, not a project dependency
    python bench/release/demo/record_tui.py --out bench/release/demo \
        --chromium /path/to/chrome
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

RUNS = ("baseline-a", "baseline-b", "degraded")  # recorded oldest first; degraded is latest
SIZE = (112, 36)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _backend(store: Path):
    import uvicorn

    from backend.app.core.config import Settings
    from backend.app.main import create_app

    app = create_app(settings=Settings(database_path=store / "demo.sqlite3"))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    os.environ["CHANGE_ASSURANCE_API_TOKEN"] = app.state.api_token
    return server, f"http://127.0.0.1:{port}"


def _seed(api_url: str, scratch: Path) -> None:
    from backend.app.cli.client import ApiClient

    repo = scratch / "mathops"
    repo.mkdir()
    (repo / "mathops.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=Demo", "-c", "user.email=demo@example.invalid", "-c",
                  "commit.gpgsign=false", "commit", "-qm", "baseline"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    client = ApiClient(api_url)
    client.create_change("Fix add() in mathops", "add(a, b) must return a + b", str(repo))
    results = ROOT / "bench" / "regression_detection" / "results"
    for name in RUNS:
        client.record_eval_run(json.loads((results / f"{name}.json").read_text(encoding="utf-8")))


async def _frames(api_url: str, frames: Path) -> list[Path]:
    from textual.widgets import Static

    from backend.app.tui.app import ChangeDashboard
    from backend.app.tui.eval_screen import EvalScreen

    shots: list[Path] = []
    app = ChangeDashboard(api_url=api_url)
    async with app.run_test(size=SIZE) as pilot:
        async def wait(predicate, timeout: float = 15.0) -> None:
            deadline = time.monotonic() + timeout
            while not predicate() and time.monotonic() < deadline:
                await pilot.pause(0.05)

        def shot(name: str, repeat: int = 1) -> None:
            path = Path(app.save_screenshot(filename=f"{name}.svg", path=str(frames)))
            shots.extend([path] * repeat)

        await wait(lambda: "Change(s)" in str(app.query_one("#status", Static).render()))
        shot("01-dashboard", 3)
        await pilot.press("e")
        await wait(lambda: isinstance(app.screen, EvalScreen))
        view = app.screen.query_one("#eval_view", Static)
        await wait(lambda: "Latest vs previous" in str(view.render()))
        shot("02-evals", 6)
    return shots


def _rasterize(svgs: list[Path], chromium: str) -> list[Path]:
    script = """
const { chromium } = require(process.argv[1]);
(async () => {
  const browser = await chromium.launch({ executablePath: process.argv[2] });
  const page = await browser.newPage({ viewport: { width: 1180, height: 760 } });
  for (const svg of process.argv.slice(3)) {
    await page.goto('file://' + svg);
    await page.locator('svg').first().screenshot({ path: svg.replace(/\\.svg$/, '.png') });
  }
  await browser.close();
})();
"""
    unique = sorted({str(path) for path in svgs})
    playwright = ROOT / "apps" / "desktop" / "node_modules" / "playwright-core"
    subprocess.run(["node", "-e", script, str(playwright), chromium, *unique], check=True)
    return [Path(str(path)[:-4] + ".png") for path in svgs]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chromium", required=True)
    args = parser.parse_args()
    from PIL import Image

    with tempfile.TemporaryDirectory(prefix="sentinel-demo-") as directory:
        scratch = Path(directory)
        server, api_url = _backend(scratch / "store")
        try:
            _seed(api_url, scratch)
            frames = scratch / "frames"
            frames.mkdir()
            pngs = _rasterize(asyncio.run(_frames(api_url, frames)), args.chromium)
        finally:
            server.should_exit = True
        images = [Image.open(path).convert("RGB") for path in pngs]
        width = max(image.width for image in images)
        height = max(image.height for image in images)
        canvas = [Image.new("RGB", (width, height), images[0].getpixel((0, 0))) for _ in images]
        for frame, image in zip(canvas, images):
            frame.paste(image, (0, 0))
        args.out.mkdir(parents=True, exist_ok=True)
        target = args.out / "sentinel-tui-evals.gif"
        canvas[0].save(target, save_all=True, append_images=canvas[1:], duration=700, loop=0,
                       optimize=True)
        print(target, f"{target.stat().st_size} bytes", f"{width}x{height}", len(canvas), "frames")


if __name__ == "__main__":
    main()
