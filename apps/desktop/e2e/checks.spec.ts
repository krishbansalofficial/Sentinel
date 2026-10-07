import { expect, test, type Page } from "@playwright/test";
import type { CheckRunView } from "../src/lib/api/types";
import { installFakeApi, makeChange } from "./fake-api";

const change = makeChange(1, { title: "Confined checks", files: 1 });
const NOW = "2026-10-04T12:00:00Z";
const RUN = "11111111-0000-0000-0000-000000000001";
const WINDOWS_RUN = "22222222-0000-0000-0000-000000000002";
const UNKNOWN_RUN = "33333333-0000-0000-0000-000000000003";
const UNCONFINED_RUN = "44444444-0000-0000-0000-000000000004";

function run(id = RUN, overrides: Partial<CheckRunView> = {}): CheckRunView {
  return {
    id, change_id: change.id, state: "CLEANED", boundary: "LINUX_SANDBOX",
    network: false, exit_code: 0, timed_out: false, created_at: NOW, updated_at: NOW,
    linux_sandbox: {
      verified: true, separate_namespaces: ["user", "mnt", "pid", "net", "ipc", "uts"],
      seccomp_mode: "2", seccomp_filters_added: 2, no_new_privs: true,
      cgroup: "/sentinel/check-1", network_isolated: true,
    },
    ...overrides,
  };
}

async function setup(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => sessionStorage.setItem("ca.dev.token", "t"));
  await installFakeApi(page, {
    changes: [change], actors: [{ id: "actor-1", display_name: "Operator", kind: "HUMAN" }],
  });
  return errors;
}

const table = (page: Page) => page.getByRole("table", { name: "Check runs", exact: true });
const section = (page: Page) => page.locator("section").filter({
  has: page.getByRole("heading", { name: "Check runs", exact: true }),
});

test("Assurance renders Linux, Windows, unknown and unconfined check runs honestly", async ({ page }) => {
  const errors = await setup(page);
  await page.route("**/changes/*/checks", route => route.fulfill({ json: { count: 4, items: [
    run(),
    run(WINDOWS_RUN, { boundary: "APPCONTAINER", linux_sandbox: null, network: true,
      token: { package_sid: "S-1-15-2-1", is_appcontainer: true, job_verified: true, integrity_rid: "0x1000" } }),
    run(UNKNOWN_RUN, { boundary: null, network: null, exit_code: null,
      linux_sandbox: { verified: false, no_new_privs: false, network_isolated: false } }),
    run(UNCONFINED_RUN, { boundary: "UNCONFINED", linux_sandbox: null, timed_out: true, exit_code: null }),
  ] } }));
  await page.goto(`/changes/${change.id}/assurance`);
  const linux = table(page).getByRole("row").filter({ hasText: "11111111" });
  await expect(linux).toContainText("Linux sandbox box");
  await expect(linux).toContainText("Verified before the check ran");
  await expect(linux).toContainText("Separate namespaces: user, mnt, pid, net, ipc, uts");
  await expect(linux).toContainText("Seccomp filter mode, 2 filters added");
  await expect(linux).toContainText("Network isolated");
  await expect(linux.getByRole("cell").nth(3)).toHaveText("Off");
  await expect(linux.getByRole("cell").nth(4)).toHaveText("0");
  const windows = table(page).getByRole("row").filter({ hasText: "22222222" });
  await expect(windows).toContainText("AppContainer box");
  await expect(windows).toContainText("Token and Job Object verified");
  await expect(windows.getByRole("cell").nth(3)).toHaveText("On");
  const unknown = table(page).getByRole("row").filter({ hasText: "33333333" });
  await expect(unknown).toContainText("Not verified");
  await expect(unknown).toContainText("Verification failed");
  await expect(unknown.getByRole("cell").nth(3)).toHaveText("Unknown");
  await expect(unknown.getByRole("cell").nth(4)).toHaveText("—");
  await expect(unknown).not.toContainText("Verified before the check ran");
  const unconfined = table(page).getByRole("row").filter({ hasText: "44444444" });
  await expect(unconfined).toContainText("Unconfined");
  await expect(unconfined.getByRole("cell").nth(4)).toHaveText("timed out");
  expect(errors).toEqual([]);
});

test("check runs show loading, then the empty state without claiming a pass", async ({ page }) => {
  await setup(page);
  let release!: () => void;
  const ready = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/changes/*/checks", async route => {
    await ready;
    await route.fulfill({ json: { count: 0, items: [] } });
  });
  await page.goto(`/changes/${change.id}/assurance`);
  await expect(page.getByRole("status", { name: "Loading check runs" })).toBeVisible();
  release();
  await expect(section(page).getByText("No check runs", { exact: true })).toBeVisible();
  await expect(table(page)).toHaveCount(0);
  await expect(section(page)).not.toContainText("Verified before the check ran");
});

test("a check-list error keeps its context and retries successfully", async ({ page }) => {
  await setup(page);
  let failing = true;
  await page.route("**/changes/*/checks", route => failing
    ? route.fulfill({ status: 500, json: { error: { code: "SERVER_ERROR", message: "Checks unavailable" } } })
    : route.fulfill({ json: { count: 1, items: [run()] } }));
  await page.goto(`/changes/${change.id}/assurance`);
  await expect(section(page).getByRole("alert")).toContainText("Checks unavailable");
  failing = false;
  await section(page).getByRole("button", { name: "Try again" }).click();
  await expect(table(page)).toContainText("Linux sandbox box");
});

test("running an assurance plan refreshes check runs without reloading the page", async ({ page }) => {
  const errors = await setup(page);
  let items: CheckRunView[] = [];
  let reads = 0;
  let body: unknown;
  await page.route("**/changes/*/checks", route => {
    reads++;
    return route.fulfill({ json: { count: items.length, items } });
  });
  await page.route("**/assurance/plan", route => route.fulfill({ json: {
    id: "plan-1", change_id: change.id, checkpoint_id: "checkpoint-1", created_at: NOW,
    checks: [{ id: "check-1", name: "Pytest", executable: "pytest", args: ["-q"], required: true }],
    coverage_gaps: [],
  } }));
  await page.route("**/assurance/plan-1/evaluation", route => route.fulfill({ status: 404,
    json: { error: { code: "NOT_FOUND", message: "No evaluation yet" } } }));
  await page.route("**/assurance/plan-1/run", route => {
    body = route.request().postDataJSON();
    items = [run()];
    return route.fulfill({ json: { count: 0, items: [] } });
  });
  await page.goto(`/changes/${change.id}/assurance`);
  await expect(section(page).getByText("No check runs", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Run checks", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Run as", { exact: true }).selectOption("actor-1");
  await dialog.getByRole("button", { name: "Run checks", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(table(page)).toContainText("Linux sandbox box");
  expect(body).toEqual({ actor_id: "actor-1" });
  expect(reads).toBeGreaterThan(1);
  expect(errors).toEqual([]);
});

test("Passport v2 shows signed check boundaries and updates only when issued again", async ({ page }) => {
  const errors = await setup(page);
  let checkRuns = [
    { check_run_id: RUN, boundary: "LINUX_SANDBOX" },
    { check_run_id: UNKNOWN_RUN, boundary: null },
    { check_run_id: UNCONFINED_RUN, boundary: "UNCONFINED" },
  ];
  let issues = 0;
  await page.route("**/passport/v2/issue", route => {
    issues++;
    return route.fulfill({ json: {
      payload_digest: "a".repeat(64), signer_fingerprint: "test-signer",
      payload: { change_id: change.id, execution_boundary: "LINUX_SANDBOX",
        confined_checks: checkRuns.length === 1 ? "PASS" : "FAIL", check_runs: checkRuns,
        launch_boundaries: [], issued_at: NOW },
    } });
  });
  await page.goto(`/changes/${change.id}/passport`);
  await expect(table(page)).toHaveCount(0);
  await page.getByRole("button", { name: "Issue Passport v2", exact: true }).click();
  await expect(table(page)).toContainText("Linux sandbox box");
  await expect(table(page)).toContainText("Not verified");
  await expect(table(page)).toContainText("Unconfined");
  const v2 = page.locator("section").filter({ has: page.getByRole("heading", { name: "Passport v2", exact: true }) });
  await expect(v2.getByText("FAIL", { exact: true })).toBeVisible();
  checkRuns = [{ check_run_id: RUN, boundary: "LINUX_SANDBOX" }];
  await expect(table(page).getByRole("row")).toHaveCount(4);
  await page.getByRole("button", { name: "Issue again", exact: true }).click();
  await expect(table(page).getByRole("row")).toHaveCount(2);
  await expect(v2.getByText("PASS", { exact: true })).toBeVisible();
  expect(issues).toBe(2);
  expect(errors).toEqual([]);
});

test("the check-runs table stays contained on a narrow screen", async ({ page }) => {
  await setup(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/changes/*/checks", route => route.fulfill({ json: { count: 1, items: [run()] } }));
  await page.goto(`/changes/${change.id}/assurance`);
  await expect(table(page)).toContainText("Linux sandbox box");
  await section(page).scrollIntoViewIfNeeded();
  const bounds = await section(page).boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await table(page).evaluate(element => { element.parentElement!.scrollLeft = element.parentElement!.scrollWidth; });
  await expect(table(page).getByText("Network isolated", { exact: true })).toBeInViewport();
});

test("Assurance reads the real API's empty check list for a new Change", async ({ page, request }) => {
  const token = process.env.CA_E2E_TOKEN!;
  const created = await request.post("http://127.0.0.1:8000/api/v1/changes", {
    headers: { Authorization: `Bearer ${token}` },
    data: { title: "Check-list browser integration", intent: "Inspect confined check evidence",
      repository_path: process.env.CA_E2E_REPO! },
  });
  expect(created.status()).toBe(201);
  const realChange = await created.json();
  try {
    await page.addInitScript(value => sessionStorage.setItem("ca.dev.token", value), token);
    const checks = page.waitForResponse(response =>
      response.url().endsWith(`/changes/${realChange.id}/checks`) && response.request().method() === "GET");
    await page.goto(`/changes/${realChange.id}/assurance`);
    const response = await checks;
    expect(response.status()).toBe(200);
    expect(await response.json()).toEqual({ count: 0, items: [] });
    await expect(section(page).getByText("No check runs", { exact: true })).toBeVisible();
    await expect(section(page).getByRole("alert")).toHaveCount(0);
  } finally {
    const removed = await request.delete(`http://127.0.0.1:8000/api/v1/changes/${realChange.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(removed.ok()).toBeTruthy();
  }
});
