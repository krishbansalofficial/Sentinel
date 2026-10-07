import { spawnSync } from "node:child_process";

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
