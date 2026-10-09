# Release preparation

2026-10-04: public `https://pypi.org/pypi/sentinel-runtime/json` returned HTTP 404. That means
there is no current project metadata at that URL; it does not establish that PyPI will allow
registration. Package metadata remains `change-assurance` until maintainers select a name.
No package has been published or name reserved.

Generate the leaderboard from reviewed real result files:

```bash
sentinel eval leaderboard results/baseline.json results/candidate.json --html public/index.html
```

The generated page is self-contained and escapes document text. It exposes suite/task set,
k, config, model, errors, unknown costs and boundary labels so unmatched runs are not silently
ranked as comparable. Only summary data is published; prompts, output and tokens are omitted.
After review, configure GitHub Pages from an approved branch/folder and commit that page there.
Hosting has not been enabled or published during this implementation.

Before release: obtain csshlok's license agreement; select/register the package name; run an
authenticated Claude benchmark; capture a real walkthrough GIF; verify both CI platforms;
review the generated leaderboard; publish the package/page; then submit launch posts.

Draft launch copy (fill in only measured data):

“Sentinel supervises coding agents inside verified Windows/Linux boundaries and records
signed evidence of their changes. Its regression lab runs hidden-test tasks, reports Wilson
95% pass-rate intervals, and compares agent configurations. Here are our reproducible results:
[reviewed leaderboard URL]. Install: [released package command]. Feedback and contributions:
[repository issue URL].”

No launch messages have been sent. Adoption tracking starts with unknown values in
`bench/adoption.csv`; fill them from dated, attributable measurements after release.
