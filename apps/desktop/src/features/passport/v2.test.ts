import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { BOUNDARIES, CONTRACT_LOAD_ERRORS, boundaryClaimInfo, contractLoadErrorText } from "./v2.ts";

const spec = JSON.parse(readFileSync(new URL("../../../../../openapi.json", import.meta.url), "utf8"));
const repoContract = readFileSync(new URL("../../../../../backend/app/policy/repo_contract.py", import.meta.url), "utf8");

test("every ExecutionBoundary in the contract has wording, and no stale ones remain", () => {
  const values: string[] = spec.components.schemas.PassportV2Payload.properties.execution_boundary.enum;
  assert.ok(values.length >= 4, String(values));
  assert.deepEqual([...values].sort(), Object.keys(BOUNDARIES).sort());
  assert.deepEqual([...spec.components.schemas.PassportV2LaunchBoundary.properties.boundary.enum].sort(), Object.keys(BOUNDARIES).sort());
});

test("only a verified AppContainer reads as ok; unconfined is danger", () => {
  assert.equal(boundaryClaimInfo("APPCONTAINER").tone, "ok");
  assert.equal(boundaryClaimInfo("UNCONFINED").tone, "danger");
  assert.notEqual(boundaryClaimInfo("RESTRICTED_TOKEN").tone, "ok");
  assert.notEqual(boundaryClaimInfo("UNKNOWN").tone, "ok");
  assert.notEqual(boundaryClaimInfo("SOMETHING_NEW").tone, "ok");
  assert.match(boundaryClaimInfo("SOMETHING_NEW").label, /SOMETHING_NEW/);
  assert.equal(boundaryClaimInfo(null).label, "Unknown");
});

test("every repository-contract error code the backend raises has wording", () => {
  const codes = [...new Set([...repoContract.matchAll(/"(REPOSITORY_CONTRACT_[A-Z_]+)"/g)].map((m) => m[1] ?? ""))];
  assert.ok(codes.length === 3, String(codes));
  assert.deepEqual(codes.sort(), Object.keys(CONTRACT_LOAD_ERRORS).sort());
  assert.equal(contractLoadErrorText("OTHER", "fallback"), "fallback");
  assert.equal(contractLoadErrorText(undefined, "fallback"), "fallback");
});
