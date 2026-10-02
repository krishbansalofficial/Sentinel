import { expect, test, type Page, type Route } from "@playwright/test";
import { installFakeApi, makeChange } from "./fake-api";

const NOW = "2026-09-19T12:00:00Z";
const json = (route: Route, status: number, body: unknown) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
const err = (code: string, message: string) => ({ error: { code, message, details: {} } });

async function open(page: Page, path: string) {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(() => sessionStorage.setItem("ca.dev.token", "t"));
  const c = makeChange(1, { title: "Parity change", files: 1, revision: 3 });
  const api = await installFakeApi(page, { changes: [c] });
  return { c, api, errors, go: () => page.goto(path.replace("$id", c.id)) };
}

test("loading the repository contract sends the current revision and shows its source", async ({ page }) => {
  const { c, errors, go } = await open(page, "/changes/$id/contract");
  const bodies: unknown[] = [];
  await page.route("**/contract/from-repository", async (route) => {
    bodies.push(route.request().postDataJSON());
    return json(route, 200, { change: { id: c.id }, commit: "c".repeat(40), path: ".sentinel/contract.toml", blob_sha256: "d".repeat(64) });
  });
  await go();
  await page.getByRole("button", { name: "Load from repository" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Loaded from the repository" })).toContainText(".sentinel/contract.toml");
  expect(bodies).toEqual([{ expected_revision: 3 }]);
  expect(errors).toEqual([]);
});

test("a missing repository contract is explained, not shown as success", async ({ page }) => {
  const { go } = await open(page, "/changes/$id/contract");
  await page.route("**/contract/from-repository", (route) => json(route, 404, err("REPOSITORY_CONTRACT_MISSING", "The baseline commit has no .sentinel/contract.toml.")));
  await go();
  await page.getByRole("button", { name: "Load from repository" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "wasn't loaded" })).toContainText("has no .sentinel/contract.toml");
  await expect(page.getByText("Loaded from the repository")).toHaveCount(0);
});

test("GitLab can be connected from the GitHub page and the token is sent once", async ({ page }) => {
  const { go } = await open(page, "/github");
  let configured = false;
  const tokens: string[] = [];
  await page.route("**/api/v1/providers/gitlab/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/connect")) { tokens.push(route.request().postDataJSON().token); configured = true; }
    return json(route, 200, { provider: "gitlab", configured });
  });
  await go();
  const section = page.getByRole("region", { name: "GitLab" }).or(page.locator("section", { hasText: "gitlab.repo.read" }));
  await section.getByRole("button", { name: "Connect GitLab" }).click();
  await page.getByRole("dialog").getByLabel("Personal access token").fill("glpat-secret");
  await page.getByRole("dialog").getByRole("button", { name: "Connect" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(tokens).toEqual(["glpat-secret"]);
  await expect(section.getByText("Connected", { exact: true })).toBeVisible();
  await expect(page.getByText("glpat-secret")).toHaveCount(0);
});

test("Passport v2 shows the bound boundary claim and each launch's boundary", async ({ page }) => {
  const { c, go } = await open(page, "/changes/$id/passport");
  await page.route("**/passport/v2/issue", (route) => json(route, 200, {
    payload_digest: "e".repeat(64), signer_fingerprint: "ABCDEFGH-IJKLMNOP", signer_provider: "SOFTWARE",
    signer_identity: "Sentinel installation Lab", signer_public_spki_b64: "AA==", signature_b64: "AA==",
    payload: { change_id: c.id, execution_boundary: "RESTRICTED_TOKEN", confined_checks: "PASS",
      diff_coverage: { diff_exercised: "PASS" }, policy_decision: "DENY", policy_preset_name: "strict",
      policy_preset_version: "1", policy_denials: ["execution boundary is not APPCONTAINER"], issued_at: NOW,
      launch_boundaries: [
        { run_id: "11111111-0000-0000-0000-000000000000", boundary: "APPCONTAINER", package_sid: "S-1-15-2-1" },
        { run_id: "22222222-0000-0000-0000-000000000000", boundary: "RESTRICTED_TOKEN", package_sid: null }] },
  }));
  await go();
  await page.getByRole("button", { name: "Issue Passport v2" }).click();
  await expect(page.getByText("Restricted token").first()).toBeVisible();
  await expect(page.getByRole("table", { name: "Launch boundaries" }).getByText("AppContainer verified")).toBeVisible();
  await expect(page.getByText("execution boundary is not APPCONTAINER")).toBeVisible();
  await expect(page.getByText("DENY (strict 1)")).toBeVisible();
});
