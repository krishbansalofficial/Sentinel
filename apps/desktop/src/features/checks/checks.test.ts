import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { CHECK_BOUNDARIES, checkBoundaryInfo, linuxFactsLines, networkText } from "./checks.ts";

const spec = JSON.parse(readFileSync(new URL("../../../../../openapi.json", import.meta.url), "utf8"));

function boundaryEnum(schema: string): string[] {
  const property = spec.components.schemas[schema].properties.boundary;
  const options = property.anyOf ?? [property];
  return options.flatMap((option: { enum?: string[] }) => option.enum ?? []);
}

test("every check-run boundary in the contract has wording, and no stale ones remain", () => {
  for (const schema of ["CheckRunView", "PassportV2CheckRun", "VerificationResult"]) {
    assert.deepEqual([...boundaryEnum(schema)].sort(), Object.keys(CHECK_BOUNDARIES).sort(), schema);
  }
});

test("each box keeps its own name; a missing boundary is never shown as confined", () => {
  assert.equal(checkBoundaryInfo("LINUX_SANDBOX").tone, "ok");
  assert.equal(checkBoundaryInfo("APPCONTAINER").tone, "ok");
  assert.notEqual(checkBoundaryInfo("LINUX_SANDBOX").label, checkBoundaryInfo("APPCONTAINER").label);
  assert.doesNotMatch(checkBoundaryInfo("LINUX_SANDBOX").label, /AppContainer/);
  assert.equal(checkBoundaryInfo("UNCONFINED").tone, "danger");
  for (const missing of [null, undefined, ""]) assert.equal(checkBoundaryInfo(missing).tone, "warn");
  assert.match(checkBoundaryInfo("SOMETHING_NEW").label, /Unrecognized/);
});

test("Linux facts read as verified lines, and failures say so", () => {
  const lines = linuxFactsLines({
    verified: true, separate_namespaces: ["user", "mnt", "pid", "net"], seccomp_mode: "2",
    seccomp_filters_added: 1, no_new_privs: true, cgroup: "/sentinel/sentinel-run-abc", network_isolated: true,
  });
  assert.deepEqual(lines, [
    "Verified before the check ran", "Separate namespaces: user, mnt, pid, net",
    "Seccomp filter mode, 1 filter added", "no_new_privs set", "Network isolated", "Cgroup /sentinel/sentinel-run-abc",
  ]);
  const failed = linuxFactsLines({ verified: false, seccomp_mode: "0", seccomp_filters_added: 0, no_new_privs: false, network_isolated: false });
  assert.deepEqual(failed, ["Verification failed", "No separate namespaces recorded", "No seccomp filter recorded", "no_new_privs not set", "Network shared"]);
  assert.deepEqual(linuxFactsLines(null), []);
});

test("an unrecorded network flag is unknown, not off", () => {
  assert.equal(networkText(false), "Off");
  assert.equal(networkText(true), "On");
  assert.equal(networkText(null), "Unknown");
});
