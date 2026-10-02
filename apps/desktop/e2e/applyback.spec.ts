import { expect, test, type Page, type Route } from "@playwright/test";
import { installFakeApi, makeChange } from "./fake-api";

const NOW = "2026-09-19T12:00:00Z";
const ACTOR = { id: "actor-1-0000", display_name: "Owner", kind: "HUMAN" };

const workspace = (over: Record<string, unknown> = {}) => ({
  id: "ws-1", change_id: "chg-0001", state: "READY", profile_name: "sentinel.ws.test",
  package_sid: "S-1-15-2-1", workspace_path: "C:\\ws", source_repository: "C:\\work\\repo-1",
  base_branch: "main", base_sha: "a".repeat(40), sealed_sha: null, applied_sha: null,
  refusal_reason: null, active_run_id: null, credential_staged: false, limitations: [],
  created_at: NOW, updated_at: NOW, cleaned_at: null,
  runs: [{ run_id: "run-00000001", status: "PASSED", limitations: [], finished_at: NOW,
           boundary: { profile_name: "sentinel.ws.test", package_sid: "S-1-15-2-1", is_appcontainer: true,
                       integrity_rid: "0x1000", capability_sids: [], job_verified: true, verified_at: NOW } }],
  ...over,
});

const preview = (over: Record<string, unknown> = {}) => ({
  change_id: "chg-0001", workspace_id: "ws-1", base_sha: "a".repeat(40), sealed_sha: "b".repeat(40),
  commits: [], commits_truncated: false, patch: "", patch_truncated: false, limitations: [],
  user_branch: "main", user_head: "a".repeat(40), approval_token: "approve-me", refusal_reason: null,
  fast_forward_possible: true,
  changed_paths: [{ status: "M", path: "calc.py", old_mode: "100644", new_mode: "100644", flags: [] }],
  ...over,
});

interface Script { workspace?: unknown; workspaceStatus?: number; preset?: unknown; preview?: unknown; applied?: boolean }

async function open(page: Page, script: Script) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(() => sessionStorage.setItem("ca.dev.token", "t"));
  const c = makeChange(1, { title: "Apply change", files: 1 });
  await installFakeApi(page, { changes: [c], actors: [ACTOR] });
  const calls: { path: string; body: any }[] = [];
  const json = (route: Route, status: number, body: unknown) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  // Registered after the fake API, so these win for the routes this tab uses.
  await page.route("**/api/v1/changes/*/policy/preset", (route) => json(route, 200, script.preset ?? { change_id: c.id, decision: "DENY", denials: ["No versioned policy preset is selected."], freshness: "UNKNOWN", preset_name: null }));
  await page.route("**/api/v1/changes/*/workspace**", async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const body = req.postData() ? req.postDataJSON() : undefined;
    if (req.method() !== "GET") calls.push({ path, body });
    if (path.endsWith("/workspace")) {
      return script.workspaceStatus === 404
        ? json(route, 404, { error: { code: "WORKSPACE_NOT_FOUND", message: "No live workspace exists for this Change.", details: {} } })
        : json(route, 200, script.workspace ?? workspace());
    }
    if (path.endsWith("/preview")) return json(route, 200, script.preview ?? preview());
    if (path.endsWith("/apply")) return json(route, 200, { applied: script.applied ?? true, workspace: workspace({ state: "APPLIED", applied_sha: "b".repeat(40) }), preview: null });
    if (path.endsWith("/discard")) return json(route, 200, workspace({ state: "DISCARDED" }));
    return json(route, 404, {});
  });
  await page.goto(`/changes/${c.id}/apply`);
  return { calls, errors };
}

test("the tab is reachable and explains a Change without a workspace", async ({ page }) => {
  const { errors } = await open(page, { workspaceStatus: 404 });
  await expect(page.getByRole("navigation", { name: "Change sections" }).getByRole("link", { name: "Apply-back" })).toHaveAttribute("aria-current", "page");
  await expect(page.getByText("No workspace", { exact: true })).toBeVisible();
  await expect(page.getByText("No preset selected")).toBeVisible();
  expect(errors).toEqual([]);
});

test("run boundaries and a denied preset are shown as they are", async ({ page }) => {
  await open(page, { preset: { change_id: "chg-0001", decision: "DENY", denials: ["confined checks are not PASS"], freshness: "CURRENT", preset_name: "strict", preset_version: "1" } });
  await expect(page.getByText("AppContainer verified")).toBeVisible();
  await expect(page.getByText("Denied", { exact: true })).toBeVisible();
  await expect(page.getByText("confined checks are not PASS")).toBeVisible();
});

test("a refused preview names the forbidden path and offers no apply", async ({ page }) => {
  const { calls } = await open(page, { preview: preview({
    approval_token: null, refusal_reason: "FORBIDDEN_PATH_IN_DIFF", fast_forward_possible: false,
    changed_paths: [{ status: "M", path: ".github/workflows/ci.yml", old_mode: "100644", new_mode: "100644", flags: ["forbidden"] }],
  }) });
  await page.getByRole("button", { name: "Preview apply-back" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Apply-back is refused" })).toContainText("Change Contract forbids");
  await expect(page.getByText("Forbidden paths in the changes")).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply to branch…" })).toHaveCount(0);
  expect(calls.map((c) => c.path.split("/").pop())).toEqual(["preview"]);
});

test("apply needs an actor and the branch name typed exactly", async ({ page }) => {
  const { calls } = await open(page, {});
  await page.getByRole("button", { name: "Preview apply-back" }).click();
  await page.getByRole("button", { name: "Apply to branch…" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Apply" }).click();
  await expect(dialog).toContainText("Choose who holds the workspace.apply delegation.");
  await expect(dialog).toContainText("Type main to confirm.");
  await dialog.getByLabel("Applied by").selectOption({ value: ACTOR.id });
  await dialog.getByLabel("Type main to confirm").fill("Main");
  await dialog.getByRole("button", { name: "Apply" }).click();
  await expect(dialog).toContainText("Type main to confirm.");
  expect(calls.some((c) => c.path.endsWith("/apply"))).toBe(false);
  await dialog.getByLabel("Type main to confirm").fill("main");
  await dialog.getByRole("button", { name: "Apply" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const apply = calls.find((c) => c.path.endsWith("/apply"));
  expect(apply?.body).toEqual({ actor_id: ACTOR.id, approval_token: "approve-me" });
});

test("discard needs an actor and posts only the actor", async ({ page }) => {
  const { calls } = await open(page, {});
  await page.getByRole("button", { name: "Discard…" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Discard" }).click();
  await expect(dialog).toContainText("Choose who holds the workspace.discard delegation.");
  await dialog.getByLabel("Discarded by").selectOption({ value: ACTOR.id });
  await dialog.getByRole("button", { name: "Discard" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(calls.find((c) => c.path.endsWith("/discard"))?.body).toEqual({ actor_id: ACTOR.id });
});
