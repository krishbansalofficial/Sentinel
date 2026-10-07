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

test("signature verification uses the system host, isolated modules and encoded literal paths", async () => {
  const { verifyPythonSignature } = await import("../../scripts/packaging/stage-policy.mjs");
  const exe = "D:\\work'$(whoami)\\python.exe";
  const signature = { status: "Valid", subject: "CN=Python Software Foundation" };
  const result = verifyPythonSignature(exe, {
    env: { SystemRoot: "C:\\Windows", PSModulePath: "C:\\PowerShell7\\Modules", PATH: "C:\\tools" },
    spawn(host, args, options) {
      assert.equal(host, "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe");
      assert.equal(options.env.PSModulePath, undefined);
      assert.equal(options.windowsHide, true);
      assert.equal(options.timeout, 30_000);
      assert.equal(args.at(-2), "-EncodedCommand");
      const script = Buffer.from(args.at(-1), "base64").toString("utf16le");
      assert.ok(script.includes("$ErrorActionPreference = 'Stop'"));
      assert.ok(script.includes("Microsoft.PowerShell.Security.psd1"));
      assert.ok(script.includes("-LiteralPath 'D:\\work''$(whoami)\\python.exe'"));
      return { status: 0, stdout: JSON.stringify(signature), stderr: "" };
    },
  });
  assert.deepEqual(result, signature);
});

test("signature verification fails closed on subprocess errors, malformed output and wrong signers", async () => {
  const { verifyPythonSignature } = await import("../../scripts/packaging/stage-policy.mjs");
  for (const result of [
    { status: 1, stdout: "", stderr: "security module could not load" },
    { status: 0, stdout: "", stderr: "" },
    { status: 0, stdout: JSON.stringify({ status: "NotSigned", subject: "Python Software Foundation" }) },
    { status: 0, stdout: JSON.stringify({ status: "Valid", subject: "Another publisher" }) },
  ]) assert.throws(() => verifyPythonSignature("C:\\python.exe", { spawn: () => result }));
  assert.throws(() => verifyPythonSignature("C:\\python.exe", { spawn: () => ({ status: null, error: new Error("spawn failed") }) }), /spawn failed/);
});
