import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { REFUSALS, boundaryInfo, canApply, confirmationMatches, flaggedPaths, presetInfo, refusalText } from "./applyback.ts";

const backendModels = readFileSync(new URL("../../../../../backend/app/workspace/models.py", import.meta.url), "utf8");
const applyRefusals = (): string[] => {
  const block = backendModels.split("class ApplyRefusal(StrEnum):")[1]?.split("\n\n\n")[0] ?? "";
  return [...block.matchAll(/^\s+([A-Z_]+) = "([A-Z_]+)"/gm)].map((m) => m[2] ?? "");
};

test("every backend ApplyRefusal has human wording, and no stale labels remain", () => {
  const backend = applyRefusals();
  assert.ok(backend.length >= 9, String(backend));
  assert.deepEqual([...backend].sort(), Object.keys(REFUSALS).sort());
  for (const reason of backend) assert.ok(refusalText(reason)!.length > 10, reason);
});

test("unknown refusals still say something, and no refusal says nothing", () => {
  assert.match(refusalText("SOMETHING_NEW")!, /SOMETHING_NEW/);
  assert.equal(refusalText(null), null);
  assert.equal(refusalText(""), null);
});

test("apply is offered only for a refusal-free preview with a token", () => {
  assert.equal(canApply({ approval_token: "t", refusal_reason: null, fast_forward_possible: true }), true);
  assert.equal(canApply({ approval_token: "t", refusal_reason: "FORBIDDEN_PATH_IN_DIFF" }), false);
  assert.equal(canApply({ approval_token: null, refusal_reason: null }), false);
  assert.equal(canApply({ approval_token: "", refusal_reason: null }), false);
  assert.equal(canApply({ approval_token: "t", fast_forward_possible: false }), false);
  assert.equal(canApply(null), false);
  assert.equal(canApply(undefined), false);
});

test("flagged paths are picked out per flag", () => {
  const preview = { changed_paths: [{ path: "a.py", flags: ["forbidden"] }, { path: "b.env", flags: ["credential", "forbidden"] }, { path: "c.md", flags: [] }, { path: "d", flags: null }] };
  assert.deepEqual(flaggedPaths(preview, "forbidden"), ["a.py", "b.env"]);
  assert.deepEqual(flaggedPaths(preview, "credential"), ["b.env"]);
  assert.deepEqual(flaggedPaths(null, "forbidden"), []);
});

test("a boundary is verified only when every fact holds", () => {
  assert.equal(boundaryInfo({ is_appcontainer: true, job_verified: true, integrity_rid: "0x1000" }).tone, "ok");
  assert.equal(boundaryInfo({ is_appcontainer: true, job_verified: false, integrity_rid: "0x1000" }).tone, "danger");
  assert.equal(boundaryInfo({ is_appcontainer: false, job_verified: true, integrity_rid: "0x1000" }).tone, "danger");
  assert.equal(boundaryInfo({ is_appcontainer: true, job_verified: true, integrity_rid: "0x2000" }).tone, "danger");
  assert.equal(boundaryInfo(null).tone, "warn");
});

test("preset decision wording never reads as allowed unless it is", () => {
  assert.equal(presetInfo("ALLOW", "strict").tone, "ok");
  assert.equal(presetInfo("DENY", "strict").tone, "danger");
  assert.equal(presetInfo(undefined, "standard").tone, "danger");
  assert.equal(presetInfo("DENY", null).label, "No preset selected");
});

test("the typed confirmation must name the branch exactly", () => {
  assert.equal(confirmationMatches("main", "main"), true);
  assert.equal(confirmationMatches(" main ", "main"), true);
  assert.equal(confirmationMatches("Main", "main"), false);
  assert.equal(confirmationMatches("", "main"), false);
  assert.equal(confirmationMatches("main", null), false);
});
