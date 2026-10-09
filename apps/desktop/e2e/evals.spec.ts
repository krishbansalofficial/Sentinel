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
