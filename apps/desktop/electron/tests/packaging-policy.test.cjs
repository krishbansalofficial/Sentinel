const { test } = require("node:test");
const assert = require("node:assert/strict");
const { mkdtempSync, writeFileSync, rmSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");

test("backend staging excludes stores, secrets and generated files", async () => {
  const { backendSourceAllowed } = await import("../../scripts/packaging/stage-policy.mjs");
  for (const path of ["backend/app/.change-assurance", "backend/app/.change-assurance/api_token", "backend/.env.local",
    "backend/runtime.sqlite3", "backend/db.db", "backend/tests", "backend/app/__pycache__/main.pyc"]) {
    assert.equal(backendSourceAllowed(path), false, path);
    assert.equal(backendSourceAllowed(path.replaceAll("/", "\\")), false, path);
  }
  assert.equal(backendSourceAllowed("backend/app/main.py"), true);
});

test("packaging reads complete pinned requirements and refuses loose versions", async () => {
  const { readRuntimeRequirements } = await import("../../scripts/packaging/stage-policy.mjs");
  const scratch = mkdtempSync(join(tmpdir(), "sentinel-packaging-"));
  try {
    const metadata = join(scratch, "pyproject.toml");
    writeFileSync(metadata, '[project]\ndependencies = ["fastapi==0.141.1", "cryptography==47.0.0"]\n');
    assert.deepEqual(readRuntimeRequirements(metadata, process.env.CHANGE_ASSURANCE_PYTHON || "python"), ["fastapi==0.141.1", "cryptography==47.0.0"]);
    writeFileSync(metadata, '[project]\ndependencies = ["fastapi>=0.115"]\n');
    assert.throws(() => readRuntimeRequirements(metadata, process.env.CHANGE_ASSURANCE_PYTHON || "python"), /exact version pins/);
  } finally { rmSync(scratch, { recursive: true, force: true }); }
});

test("PowerShell paths remain literals with apostrophes and command syntax", async () => {
  const { powershellLiteral } = await import("../../scripts/packaging/stage-policy.mjs");
  assert.equal(powershellLiteral("C:\\work'$(whoami)\\python.exe"), "'C:\\work''$(whoami)\\python.exe'");
});
