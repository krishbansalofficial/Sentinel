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

Before release: record license metadata preserving upstream attribution; select/register the package name; run an
authenticated Claude benchmark; verify both CI platforms;
review the generated leaderboard; publish the package/page; then submit launch posts.

Draft launch copy (fill in only measured data):

“Sentinel supervises coding agents inside verified Windows/Linux boundaries and records
signed evidence of their changes. Its regression lab runs hidden-test tasks, reports Wilson
95% pass-rate intervals, and compares agent configurations. Here are our reproducible results:
[reviewed leaderboard URL]. Install: [released package command]. Feedback and contributions:
[repository issue URL].”

No launch messages have been sent. Adoption tracking starts with unknown values in
`bench/adoption.csv`; fill them from dated, attributable measurements after release.


Fork verification on 2026-10-06: `claude-local.json` and `claude-local.html` record
one real authenticated `op-add` attempt. The agent launched under a verified AppContainer,
but the provider refused it because the account's weekly quota was exhausted. Cost
was reported as zero. This is an error result, not a model benchmark. Repeat when
provider quota is available.

`claude-local-launch.json` preserves the sanitized backend launch record. No
Passport was issued after the failed run, so the evaluation report's agent
boundary is null and its hidden-check result is absent. The separate launch
record describes the verified AppContainer token and Job Object; it does not
claim a completed benchmark or a signed Passport.

A real packaged Electron screen tour was captured on 2026-10-06: `walkthrough.gif`
and the original `walkthrough/*.png` screens. The isolated profile contains no
synthetic benchmark results or user repositories.
