import { spawnSync } from "node:child_process";
import { win32 } from "node:path";

export function readRuntimeRequirements(metadataPath, python = "python") {
  const result = spawnSync(python, ["-I", "-c",
    "import json,sys,tomllib; print(json.dumps(tomllib.load(open(sys.argv[1], 'rb'))['project']['dependencies']))",
    metadataPath,
  ], { encoding: "utf8", windowsHide: true });
  if (result.error || result.status !== 0) throw new Error("Could not read backend dependency metadata with the configured Python.");
  const dependencies = JSON.parse(result.stdout);
  if (!Array.isArray(dependencies) || !dependencies.length || dependencies.some(
    (value) => typeof value !== "string" || !/^[\w.-]+(?:\[[\w,.-]+\])?==[^\s;]+(?:\s*;.*)?$/.test(value),
  )) throw new Error("Backend runtime dependencies must use exact version pins.");
  return dependencies;
}

export function backendSourceAllowed(source) {
  return !/\/(tests|\.change-assurance)(\/|$)|__pycache__|\.pyc$|\.md$|(^|\/)(api_token|\.env[^/]*)(\/|$)|\.(sqlite3?|db)$/i.test(source.replaceAll("\\", "/"));
}

export const powershellLiteral = (value) => `'${String(value).replaceAll("'", "''")}'`;

/** Use Windows PowerShell's own security module, independently of the caller's shell. */
export function verifyPythonSignature(exe, { spawn = spawnSync, env = process.env } = {}) {
  const host = win32.join(env.SystemRoot || "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
  const childEnv = Object.fromEntries(Object.entries(env).filter(([key]) => key.toLowerCase() !== "psmodulepath"));
  const script = `$ErrorActionPreference = 'Stop'; Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1') -Force; $s = Get-AuthenticodeSignature -LiteralPath ${powershellLiteral(exe)}; @{ status = $s.Status.ToString(); subject = $s.SignerCertificate.Subject } | ConvertTo-Json -Compress`;
  const result = spawn(host, ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")], {
    encoding: "utf8", env: childEnv, windowsHide: true, timeout: 30_000,
  });
  if (result.error || result.status !== 0) {
    throw new Error(`Python signature subprocess failed (exit ${result.status}): ${result.error?.message || result.stderr?.trim() || "no diagnostic output"}`);
  }
  let signature;
  try { signature = JSON.parse(result.stdout.trim().replace(/^\uFEFF/, "")); }
  catch { throw new Error(`Python signature subprocess returned invalid JSON: ${result.stderr?.trim() || "no diagnostic output"}`); }
  if (signature.status !== "Valid" || !/Python Software Foundation/i.test(signature.subject || "")) {
    throw new Error(`python.exe signature check failed: status=${signature.status} subject=${signature.subject || ""}`);
  }
  return signature;
}
