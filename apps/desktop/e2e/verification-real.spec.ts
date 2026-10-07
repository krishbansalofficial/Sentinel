import { expect, test } from "@playwright/test";

test("verification dialog runs a real delegated check through the backend", async ({ page }) => {
  test.skip(process.env.SENTINEL_E2E_REAL_CHECKS !== "1", "Requires a local Windows host with usable AppContainer support.");
  test.setTimeout(120_000);
  const headers = { Authorization: `Bearer ${process.env.CA_E2E_TOKEN}`, "Content-Type": "application/json" };
  const api = async (path: string, body?: unknown) => {
    const response = await fetch(`http://127.0.0.1:8000/api/v1${path}`, { method: "POST", headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    expect(response.ok, `${path}: ${await response.clone().text()}`).toBeTruthy();
    return response.json();
  };
  const change = await api("/changes", { title: "Real dialog verification", intent: "Exercise the delegated UI contract", repository_path: process.env.CA_E2E_REPO });
  try {
    const actor = await api("/actors", { kind: "HUMAN", display_name: "Verification dialog operator" });
    const grantor = await api("/actors", { kind: "HUMAN", display_name: "Verification dialog grantor" });
    await api("/delegations", { grantor_id: grantor.id, grantee_id: actor.id, change_id: change.id, scopes: ["change.legacy_verify"], ttl_seconds: 600 });
    await api(`/changes/${change.id}/evidence/baseline`);
    await page.addInitScript((token) => sessionStorage.setItem("ca.dev.token", token), process.env.CA_E2E_TOKEN!);
    await page.goto(`/changes/${change.id}`);
    await page.getByRole("button", { name: "Run verification" }).click();
    await page.getByLabel("Run as").selectOption(actor.id);
    await page.getByLabel("Command").fill("python");
    await page.getByLabel("Arguments").fill('-c "print(2 + 3)"');
    await page.getByRole("dialog").getByRole("button", { name: "Run", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0, { timeout: 90_000 });
    const result = await fetch(`http://127.0.0.1:8000/api/v1/changes/${change.id}`, { headers }).then((r) => r.json());
    expect(result.verification.status).toBe("PASSED");
    expect(result.verification.stdout.trim()).toBe("5");
    expect(result.verification.boundary).toBe("APPCONTAINER");
  } finally {
    await fetch(`http://127.0.0.1:8000/api/v1/changes/${change.id}`, { method: "DELETE", headers });
  }
});
