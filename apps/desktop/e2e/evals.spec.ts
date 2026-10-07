import { expect, test } from "@playwright/test";

test("Eval shows recorded rates, unknown cost and task results", async ({ page }) => {
  await page.route("**/api/v1/evals/runs", route => route.fulfill({ json: { count: 1, items: [{
    id: "eval-1", suite: "seed-suite", config_name: "baseline", agent: "claude", model: null,
    k: 3, started_at: "2026-10-04T12:00:00Z", completed_at: null,
    passes: 2, attempts: 3, errors: 1, rate: 2 / 3, low: .2, high: .94,
    mean_cost_usd: null, cost_unknown: 3, wall_p50_seconds: 1, wall_p95_seconds: 2,
    tasks: [{ task_id: "op-add", passes: 2, attempts: 3, errors: 1, rate: 2 / 3, low: .2, high: .94 }],
  }] } }));
  await page.goto("/evals");
  await expect(page.getByRole("heading", { level: 1, name: "Eval" })).toBeVisible();
  await expect(page.getByText("2/3 passed", { exact: false })).toContainText("66.7% (20.0%–94.0%)");
  await expect(page.getByText("Mean cost:", { exact: false })).toContainText("Unknown");
  await expect(page.getByText("seed-suite", { exact: false })).toContainText("In progress");
  await page.getByText("Task results", { exact: true }).click();
  await expect(page.getByRole("cell", { name: "op-add", exact: true })).toBeVisible();
});

test("Eval explains how to record the first run", async ({ page }) => {
  await page.route("**/api/v1/evals/runs", route => route.fulfill({ json: { count: 0, items: [] } }));
  await page.goto("/evals");
  await expect(page.getByText("No evaluations recorded")).toBeVisible();
  await expect(page.getByText("Run sentinel eval with --record", { exact: false })).toBeVisible();
});

test("Eval reports API errors and retries", async ({ page }) => {
  let failing = true;
  await page.route("**/api/v1/evals/runs", route => failing
    ? route.fulfill({ status: 500, json: { error: { code: "SERVER_ERROR", message: "Unavailable" } } })
    : route.fulfill({ json: { count: 0, items: [] } }));
  await page.goto("/evals");
  await expect(page.getByRole("alert")).toBeVisible();
  failing = false;
  await page.getByRole("button", { name: /try again|retry/i }).click();
  await expect(page.getByText("No evaluations recorded")).toBeVisible();
});

const run = (id: string, name: string, rate: number, low: number, high: number, taskRate: number) => ({
  id, suite: "seed-suite", config_name: name, agent: "mock", model: null, k: 3,
  started_at: "2026-10-06T12:00:00Z", completed_at: "2026-10-06T12:05:00Z",
  passes: Math.round(rate * 90), attempts: 90, errors: 0, rate, low, high,
  mean_cost_usd: 0.004, cost_unknown: 0, wall_p50_seconds: 0.2, wall_p95_seconds: 0.3,
  hidden_boundaries: { LINUX_SANDBOX: 90 },
  tasks: [{ task_id: "op-add", passes: 3, attempts: 3, errors: 0, rate: taskRate, low: .4, high: 1 }],
});

test("Eval compare shows the backend's paired-bootstrap verdict", async ({ page }) => {
  await page.route("**/api/v1/evals/runs", route => route.fulfill({ json: { count: 2, items: [
    run("bbbbbbbb-0000-4000-8000-000000000002", "degraded", .52, .42, .62, 0),
    run("aaaaaaaa-0000-4000-8000-000000000001", "baseline", .86, .77, .91, 1),
  ] } }));
  let asked = "";
  await page.route("**/api/v1/evals/compare?**", route => {
    asked = new URL(route.request().url()).search;
    return route.fulfill({ json: {
      baseline_id: "aaaaaaaa-0000-4000-8000-000000000001", candidate_id: "bbbbbbbb-0000-4000-8000-000000000002",
      overall_delta: -.333, regression: true, intervals_separate: true,
      bootstrap: { mean_difference: -.333, low: -.456, high: -.211, resamples: 10000, tasks: 30 },
      tasks: [{ task_id: "op-add", rate_a: 1, rate_b: 0, delta: -1, flip: "regressed" }],
      only_in_a: [], only_in_b: [] } });
  });
  await page.goto("/evals");
  await expect(page.getByRole("status", { name: "Regression verdict" }))
    .toContainText("Regression (paired bootstrap mean -33.3 pts, 95% -45.6 pts to -21.1 pts over 30 tasks)");
  expect(asked).toContain("baseline=aaaaaaaa-0000-4000-8000-000000000001");
  expect(asked).toContain("candidate=bbbbbbbb-0000-4000-8000-000000000002");
  await expect(page.getByText("Regressed", { exact: true })).toBeVisible();
});

test("Eval attempts load on demand and keep unknown values unknown", async ({ page }) => {
  await page.route("**/api/v1/evals/runs", route => route.fulfill({ json: { count: 1, items: [
    run("aaaaaaaa-0000-4000-8000-000000000001", "baseline", .5, .1, .9, .5)] } }));
  let loads = 0;
  await page.route("**/api/v1/evals/runs/*/attempts", route => {
    loads += 1;
    return route.fulfill({ json: { count: 2, items: [
      { task_id: "op-add", attempt: 1, status: "PASSED", hidden_boundary: "LINUX_SANDBOX", wall_seconds: 0.21, cost_usd: 0.004 },
      { task_id: "op-add", attempt: 2, status: "FAILED", hidden_boundary: null, wall_seconds: null, cost_usd: null, hidden_exit_code: 1 },
    ] } });
  });
  await page.goto("/evals");
  await expect(page.getByText("Task results", { exact: true })).toBeVisible();
  expect(loads).toBe(0);
  await page.getByText("Attempts", { exact: true }).click();
  const table = page.getByRole("table", { name: "Attempts for baseline" });
  await expect(table.getByRole("cell", { name: "LINUX_SANDBOX" })).toBeVisible();
  await expect(table.getByRole("cell", { name: "Hidden tests exited 1" })).toBeVisible();
  await expect(table.getByRole("cell", { name: "Unknown" })).toHaveCount(3);
  expect(loads).toBe(1);
});
