# README demo GIF

`sentinel-tui-evals.gif` is the real terminal UI, driven by Textual's pilot, against a
disposable backend that holds three committed backtest runs
(`bench/regression_detection/results/{baseline-a,baseline-b,degraded}.json`) and one Change on
a throwaway repository. Nothing on screen is mocked; the agent in those runs is the mock agent,
so their cost is synthetic. Frames are Textual's own SVG screenshots, rasterized by Chromium.

```bash
pip install pillow   # demo-only
python bench/release/demo/record_tui.py --out bench/release/demo \
  --chromium /opt/pw-browsers/chromium-1194/chrome-linux/chrome
```

Recorded 2026-10-06 on Linux 6.18, Python 3.13.16: 1180x760, 9 frames (2 distinct), 100 KB.
