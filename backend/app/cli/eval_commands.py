"""``sentinel eval``: run a task suite against an agent config, compare runs, render reports.

Results are portable files (``sentinel-eval-run/1``), so ``compare`` works offline
and ``compare --fail-on-regression`` can gate CI. Hidden tests run confined: the
Linux sandbox on Linux, the Change's AppContainer verification on Windows (real
agents). ``--allow-unconfined-hidden-tests`` is the only way to run them as a plain
host child, and every such result says ``UNCONFINED``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

from backend.app.evals.report import (
    compare,
    load_document,
    render_html,
    run_to_document,
    summarize,
    write_document,
)

eval_app = typer.Typer(no_args_is_help=True, help="Agent Regression Lab: suites, k attempts, stats.")
EXIT_REGRESSION = 3


def _hidden_runner(agent: str, client, allow_unconfined: bool):
    from backend.app.evals.hidden import (
        SandboxHiddenTestRunner,
        UnconfinedHiddenTestRunner,
        VerificationHiddenTestRunner,
    )

    if allow_unconfined:
        return UnconfinedHiddenTestRunner()
    if sys.platform.startswith("linux"):
        from backend.app.execution.cgroups import CgroupHierarchy

        return SandboxHiddenTestRunner(CgroupHierarchy.from_environment)
    if sys.platform == "win32" and agent != "mock":
        return VerificationHiddenTestRunner(client)
    raise typer.BadParameter(
        "No confined hidden-test runner exists for this agent on this platform; pass "
        "--allow-unconfined-hidden-tests to run them as a host child (labelled UNCONFINED).")


@eval_app.command("run")
def run_command(
    suite: Path = typer.Option(Path("evals/tasks"), "--suite"),
    agent: str = typer.Option("mock", "--agent", help="mock or claude"),
    k: int = typer.Option(3, "--k", min=1, max=50),
    name: str = typer.Option(None, "--name", help="Config name (default: the agent)"),
    model: str = typer.Option(None, "--model"),
    prompt_template: str = typer.Option("{prompt}", "--prompt-template",
                                        help="Use {prompt} and {title}"),
    task: list[str] = typer.Option(None, "--task", help="Only these task ids"),
    out: Path = typer.Option(None, "--out", help="Results JSON (default results/<run>.json)"),
    html_out: Path = typer.Option(None, "--html"),
    mock_skill: float = typer.Option(0.85, "--mock-skill"),
    seed: int = typer.Option(0, "--seed"),
    allow_unconfined: bool = typer.Option(False, "--allow-unconfined-hidden-tests"),
    api_url: str = typer.Option("http://127.0.0.1:8000", "--api-url",
                                envvar="CHANGE_ASSURANCE_API_URL"),
) -> None:
    """Run every task k times and write the results file (and optionally HTML)."""
    from backend.app.evals.agents import ApiAgentDriver, MockAgentDriver
    from backend.app.evals.runner import AgentConfig, EvalRunner
    from backend.app.evals.suite import load_suite

    tasks = load_suite(suite, only=set(task) if task else None)
    client = None
    if agent == "mock":
        driver = MockAgentDriver(skill=mock_skill, seed=seed)
    elif agent == "claude":
        from backend.app.cli.client import ApiClient

        client = ApiClient(api_url)
        extra = ["--model", model] if model else []
        driver = ApiAgentDriver(client, adapter="claude", executable="claude",
                                args_for=lambda prompt: ["-p", prompt, "--output-format", "json",
                                                         *extra])
    else:
        raise typer.BadParameter("--agent must be mock or claude")
    config = AgentConfig(name or agent, agent=agent, model=model, prompt_template=prompt_template)
    runner = EvalRunner(driver, _hidden_runner(agent, client, allow_unconfined))
    run = runner.run(tasks, config=config, k=k, suite=str(suite))
    document = run_to_document(run)
    target = out or Path("results") / f"{run.id}.json"
    write_document(document, target)
    if html_out is not None:
        html_out.parent.mkdir(parents=True, exist_ok=True)
        html_out.write_text(render_html(document), encoding="utf-8")
    summary = summarize(document)
    typer.echo(json.dumps({"results": str(target), "passes": summary.passes,
                           "attempts": summary.attempts, "rate": summary.rate,
                           "interval": [summary.low, summary.high], "errors": summary.errors},
                          sort_keys=True))


@eval_app.command("compare")
def compare_command(
    run_a: Path = typer.Argument(..., help="Baseline results file"),
    run_b: Path = typer.Argument(..., help="Candidate results file"),
    fail_on_regression: bool = typer.Option(False, "--fail-on-regression"),
    json_: bool = typer.Option(False, "--json"),
    seed: int = typer.Option(None, "--seed", help="Bootstrap seed (default fixed)"),
) -> None:
    """Per-task and overall deltas, flipped tasks, and a regression verdict."""
    comparison = compare(load_document(run_a), load_document(run_b), seed=seed)
    payload = comparison.to_payload()
    if json_:
        typer.echo(json.dumps(payload, sort_keys=True))
    else:
        a, b = comparison.a, comparison.b
        typer.echo(f"A {a.config}: {a.passes}/{a.attempts} = {a.rate:.1%} "
                   f"[{a.low:.1%}, {a.high:.1%}]")
        typer.echo(f"B {b.config}: {b.passes}/{b.attempts} = {b.rate:.1%} "
                   f"[{b.low:.1%}, {b.high:.1%}]")
        if comparison.bootstrap is not None:
            boot = comparison.bootstrap
            typer.echo(f"paired bootstrap mean delta {boot.mean_difference:+.3f} "
                       f"(95% {boot.interval.low:+.3f} to {boot.interval.high:+.3f}, "
                       f"{boot.tasks} tasks)")
        for label, tasks in (("regressed", comparison.regressed), ("improved", comparison.improved)):
            if tasks:
                typer.echo(f"{label}: {', '.join(tasks)}")
        typer.echo("REGRESSION" if comparison.regression else "no significant regression")
    if fail_on_regression and comparison.regression:
        raise typer.Exit(EXIT_REGRESSION)


@eval_app.command("report")
def report_command(results: Path = typer.Argument(...),
                   html_out: Path = typer.Option(..., "--html")) -> None:
    """Render a static HTML report from a results file."""
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(render_html(load_document(results)), encoding="utf-8")
    typer.echo(str(html_out))
