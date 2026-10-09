"""Static, offline leaderboard built only from explicit results documents."""
from __future__ import annotations

import html
from collections.abc import Iterable, Mapping
from typing import Any

from backend.app.evals.report import summarize


def render_leaderboard(documents: Iterable[Mapping[str, Any]]) -> str:
    rows = []
    seen = set()
    for document in documents:
        if document["id"] in seen:
            raise ValueError(f"duplicate run: {document['id']}")
        seen.add(document["id"])
        summary = summarize(document)
        config = document["config"]
        escape = lambda value: html.escape(str(value))
        cost = "UNKNOWN" if summary.mean_cost_usd is None else f"${summary.mean_cost_usd:.4f}"
        boundaries = ", ".join(f"{name}: {count}" for name, count in sorted(summary.hidden_boundaries.items()))
        # Suite, task IDs and k remain visible: different suites are not ranked together.
        rows.append(f"<tr><td>{escape(config.get('name', ''))}</td>"
                    f"<td>{escape(config.get('agent', 'UNKNOWN'))}</td>"
                    f"<td>{escape(config.get('model') or 'UNKNOWN')}</td>"
                    f"<td>{escape(document['suite'])}<details><summary>{len(summary.tasks)} tasks, k={document['k']}</summary>{escape(', '.join(t.task_id for t in summary.tasks))}</details></td>"
                    f"<td>{summary.passes}/{summary.attempts} ({summary.rate:.1%})</td>"
                    f"<td>{summary.low:.1%}–{summary.high:.1%}</td><td>{summary.errors}</td>"
                    f"<td>{cost} ({summary.cost_unknown} unknown)</td><td>{escape(boundaries)}</td>"
                    f"<td>{escape(document['id'])}<br>{escape(document.get('completed_at') or 'INCOMPLETE')}</td></tr>")
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sentinel eval leaderboard</title><style>body{font:16px system-ui;margin:2rem;color:#18202a;background:#fff}table{border-collapse:collapse}th,td{border:1px solid #aaa;padding:.7rem;text-align:left;vertical-align:top}.table{overflow:auto}summary{cursor:pointer}</style></head><body>
<h1>Sentinel eval leaderboard</h1>
<p>Each row is one recorded run. Compare matching suites, task sets and attempt counts. Intervals are Wilson 95%. Errors count as failed attempts. Mock agents have synthetic cost and tokens; UNCONFINED results do not demonstrate containment.</p>
<div class="table"><table><caption>Agent evaluation results</caption><thead><tr>
<th>Configuration</th><th>Agent</th><th>Model</th><th>Suite</th><th>Passed</th><th>95% interval</th><th>Errors</th><th>Mean cost</th><th>Hidden-test boundaries</th><th>Run</th>
</tr></thead><tbody>""" + "\n".join(rows) + "</tbody></table></div></body></html>"
