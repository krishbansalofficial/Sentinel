// Launches the PACKAGED app (release/win-unpacked) with an isolated profile and checks the real window over the
// debug port: loads via app://, connects to its bundled backend, keeps the security posture, and shuts down cleanly.
// Usage: npm run package:dir && npm run test:electron:smoke
import { execFileSync, spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import net from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const exe = resolve(dirname(fileURLToPath(import.meta.url)), "..", "release", "win-unpacked", "Sentinel.exe");
if (!existsSync(exe)) throw new Error("Run `npm run package:dir` first.");
const PORT = 9430;
const profile = mkdtempSync(join(tmpdir(), "ca-smoke-"));
let checkRepository;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
const check = (name, ok, extra = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${extra ? "  " + extra : ""}`);
  if (!ok) failures.push(name);
};

const appEnv = { ...process.env, CHANGE_ASSURANCE_USER_DATA_DIR: profile };
delete appEnv.ELECTRON_RUN_AS_NODE;
const child = spawn(exe, [`--remote-debugging-port=${PORT}`], { env: appEnv, windowsHide: true, stdio: "ignore" });
const exited = new Promise((r) => child.once("exit", r));

async function page() {
  for (let i = 0; i < 60; i++) {
    if (child.exitCode !== null) throw new Error(`App exited before opening its window (exit ${child.exitCode}).`);
    try {
      const t = (await (await fetch(`http://127.0.0.1:${PORT}/json`, { signal: AbortSignal.timeout(2000) })).json()).find((x) => x.type === "page");
      if (t) return t;
    } catch {}
    await wait(500);
  }
  throw new Error("Window did not appear.");
}

try {
  const target = await page();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));
  let n = 0;
  const pending = new Map();
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending.has(d.id)) {
      const request = pending.get(d.id);
      pending.delete(d.id);
      clearTimeout(request.timer);
      if (d.error) request.reject(new Error(d.error.message));
      else request.resolve(d.result);
    }
  };
  ws.onclose = () => {
    for (const request of pending.values()) {
      clearTimeout(request.timer);
      request.reject(new Error("Electron debug connection closed."));
    }
    pending.clear();
  };
  const ev = (expression) =>
    new Promise((resolve, reject) => {
      const id = ++n;
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error("Electron evaluation timed out."));
      }, 65_000);
      pending.set(id, { resolve, reject, timer });
      ws.send(JSON.stringify({ id, method: "Runtime.evaluate", params: { expression, awaitPromise: true, returnByValue: true } }));
    }).then((r) => r.result?.value);

  let ready = false;
  for (let i = 0; i < 80 && !ready; i++) {
    ready = await ev("document.body.innerText.includes('Connected')");
    if (!ready) await wait(500);
  }
  check("connects to the bundled backend (chip says Connected)", ready);
  check("served from app://", String(await ev("location.href")).startsWith("app://app/"));
  check("no require/process in the renderer", (await ev("typeof require + typeof process")) === "undefinedundefined");
  const bridge = JSON.parse(await ev("JSON.stringify(Object.fromEntries(Object.entries(window.changeAssuranceDesktop).map(([k,v])=>[k,Object.keys(v).sort()])))"));
  check("bridge surface is exactly the expected one", JSON.stringify(Object.fromEntries(Object.entries(bridge).sort(([a], [b]) => a.localeCompare(b)))) === JSON.stringify({
    api: ["request"],
    diagnostics: ["openLogs"],
    exports: ["saveJson"],
    repositories: ["selectFolder"],
    runtime: ["getStatus", "restartBackend"],
    windowControls: ["close", "getState", "minimize", "onStateChanged", "toggleMaximize"],
  }));
  const status = JSON.parse(await ev("window.changeAssuranceDesktop.runtime.getStatus().then(s => JSON.stringify(s))"));
  check("packaged, managed, ready", status.packaged && status.backend.mode === "managed" && status.backend.state === "ready");
  check("token is reported only as a boolean", status.hasToken === true && !JSON.stringify(status).match(/token"?:\s*"/i));
  check("capabilities load through the bridge", (await ev("window.changeAssuranceDesktop.api.request({method:'GET',path:'/api/v1/capabilities'}).then(r=>r.status)")) === 200);
  check("non-API path rejected", (await ev("window.changeAssuranceDesktop.api.request({method:'GET',path:'/etc/passwd'}).then(r=>r.error&&r.error.code)")) === "forbidden_path");
  check("PATCH rejected", (await ev("window.changeAssuranceDesktop.api.request({method:'PATCH',path:'/api/v1/health'}).then(r=>r.error&&r.error.code)")) === "forbidden_method");
  const dom = await ev("document.documentElement.outerHTML + JSON.stringify([localStorage, sessionStorage])");
  check("no token-like secret in DOM or storage", !/Bearer /i.test(dom));

  const backendPort = new URL(status.backend.url).port;
  if (process.env.SENTINEL_SMOKE_REAL_CHECKS === "1") {
    checkRepository = mkdtempSync(join(tmpdir(), "sentinel-electron-check-"));
    const secret = join(profile, "host-secret");
    const canary = join(profile, "host-canary");
    writeFileSync(secret, "private host data");
    const testSource = `import pathlib, socket, pytest\nfrom mathops import add\n\ndef test_packaged_verification():\n    assert add(2, 3) == 5\n    with pytest.raises(PermissionError):\n        pathlib.Path(${JSON.stringify(secret)}).read_text()\n    with pytest.raises(PermissionError):\n        pathlib.Path(${JSON.stringify(canary)}).write_text('escape')\n    with pytest.raises(OSError):\n        socket.create_connection(('127.0.0.1', ${Number(backendPort)}), timeout=2)\n`;
    writeFileSync(join(checkRepository, "test_boundary.py"), testSource);
    writeFileSync(join(checkRepository, "mathops.py"), "def add(a, b):\n    return a + b\n");
    writeFileSync(join(checkRepository, ".gitignore"), "__pycache__/\n.pytest_cache/\n.coverage\n");
    const gitEnv = { ...Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith("GIT_"))), GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "NUL" };
    for (const args of [["init", "--template="], ["add", "."], ["-c", "user.name=Sentinel smoke", "-c", "user.email=smoke@example.invalid", "commit", "-m", "fixture"]]) {
      execFileSync("git", args, { cwd: checkRepository, env: gitEnv, windowsHide: true, stdio: "ignore" });
    }
    const api = async (method, path, body) => {
      const response = await ev(`window.changeAssuranceDesktop.api.request(${JSON.stringify({ method, path: "/api/v1" + path, body, timeoutMs: 60000 })})`);
      if (!response || response.status < 200 || response.status >= 300) throw new Error(`Packaged API ${path}: ${JSON.stringify(response)}`);
      return response.body;
    };
    const change = await api("POST", "/changes", { title: "Packaged verification", intent: "Check the real bundled interpreter", repository_path: checkRepository,
      contract: { schema_version: 2, diff_coverage_rule: { required: true, minimum_percent: 100, policy_version: "required-v1" } } });
    const actor = await api("POST", "/actors", { kind: "HUMAN", display_name: "Smoke operator" });
    const grantor = await api("POST", "/actors", { kind: "HUMAN", display_name: "Smoke grantor" });
    await api("POST", "/delegations", { grantor_id: grantor.id, grantee_id: actor.id, change_id: change.id, scopes: ["change.legacy_verify"], ttl_seconds: 600 });
    const baseline = await api("POST", `/changes/${change.id}/evidence/baseline`);
    const result = await api("POST", `/changes/${change.id}/verify`, { actor_id: actor.id, verification: { executable: "pytest", args: ["-q", "-p", "no:cacheprovider"], timeout_seconds: 30 } });
    check("bundled pytest runs inside verified AppContainer and denies host access", result.verification?.status === "PASSED" && result.verification?.boundary === "APPCONTAINER" && !existsSync(canary), JSON.stringify(result.verification));
    writeFileSync(join(checkRepository, "mathops.py"), "def add(a, b):\n    return a + b + 0\n");
    const current = await api("POST", `/changes/${change.id}/evidence/current`);
    const coverage = await api("POST", `/changes/${change.id}/assurance/diff-coverage`, {
      baseline_checkpoint_id: baseline.checkpoint.id, tested_checkpoint_id: current.checkpoint.id,
      test_args: ["-q", "-p", "no:cacheprovider"], rule: { required: true, minimum_percent: 100 },
    });
    check("bundled diff coverage measures the changed line in AppContainer", coverage.diff_exercised === "PASS" && coverage.collection_boundary === "APPCONTAINER_IN_PROCESS" && coverage.measured_percent === 100 && !existsSync(canary), JSON.stringify(coverage));
  }
  await new Promise((r) => { ws.send(JSON.stringify({ id: ++n, method: "Browser.close" })); setTimeout(r, 300); });
  await Promise.race([exited, wait(20_000)]);
  check("app exited after a graceful close", child.exitCode !== null);
  await wait(1500);
  const listening = await new Promise((r) => { const s = net.connect(Number(backendPort), "127.0.0.1"); s.once("connect", () => (s.destroy(), r(true))); s.once("error", () => r(false)); });
  check("bundled backend stopped with the app", !listening, `port ${backendPort}`);
} catch (e) {
  check("smoke run", false, e.message);
} finally {
  if (child.exitCode === null && child.pid) {
    try { execFileSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" }); } catch {}
  }
  rmSync(profile, { recursive: true, force: true });
  if (checkRepository) rmSync(checkRepository, { recursive: true, force: true });
}
if (failures.length) {
  console.error(`\n${failures.length} check(s) failed.`);
  process.exit(1);
}
console.log("\nElectron smoke passed.");
