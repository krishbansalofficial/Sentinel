# Handoff: confined cargo and dotnet checks (written 2026-10-03)

Repo: `C:\Users\krish\Sentinel-fork`, branch `master` (only branch), pushed to the fork
`krishbansalofficial/Sentinel` at `8cec5aa`. Push only to `origin`; `upstream` (csshlok) push is
disabled on purpose. Never add Claude/Co-Authored-By/Claude-Session lines to commits.

## Fork continuation status (2026-10-06)

This fork is independently maintained by Krish. Former team approval requirements
are superseded by the owner's implementation authorization. Cargo and .NET confined
adapters are now implemented; the design notes below describe their starting point.
Cargo uses a real Rust toolchain snapshot plus x64 MSVC linker/SDK library snapshots.
.NET snapshots its SDK/runtimes/packs and uses private NuGet/CLI directories.
Real Windows compilation and host file/store/API escape probes pass for both.
Linux uses read-only SDK binds; unavailable SDKs fail closed.
See HANDOFF.md and SECURITY_REVIEW.md for current verification and remaining limits.

## Historical goal
Make `cargo` and `dotnet` confined check toolchains, the way `go` became one in `8cec5aa`.
Today they are in `UNCONFINED_TOOLCHAINS` (`backend/app/execution/check_toolchains.py`) and run
only with a delegated `checks.unconfined` authority (boundary `UNCONFINED`).

## Pattern to copy (Go, commit 8cec5aa)
- `check_runtime.go_runtime(go_exe)`: snapshots the whole GOROOT via `_snapshot([("", root)], kind=...)`
  (content-addressed cache, refuses links/reparse points, 2 GiB limit).
- `check_toolchains._go_runtime`: builds a `BoxRuntime(snapshots, env, path_entries, executable,
  scratch_env, limitations)`; `CONFINED_TOOLCHAINS["go"] = GO_TOOLCHAIN`; removed from
  `UNCONFINED_TOOLCHAINS`; `RuntimeBuilders` gained `go` / `find_go` (+ `for_root`).
- `BoxRuntime.scratch_env` (new, `check_box.py`): name -> one plain subfolder of the box scratch,
  created per box; the variable is set to its absolute path. Reserved keys (PATH, TEMP, TMP,
  LOCALAPPDATA, SYSTEMROOT, WINDIR, COMSPEC) are refused. The box sets LOCALAPPDATA to the HOST
  folder (not writable from the box), so every tool cache must go through scratch_env.
- Tests: `backend/tests/acceptance/test_confined_go.py` (real AppContainer via the live API:
  legit `go test` passes with boundary APPCONTAINER; escapes denied with a host positive control:
  write into the user repo, read `api.store`, dial `127.0.0.1:api.port`). Took ~12 min first run
  (GOROOT snapshot). Update `test_check_toolchains.py::test_unmapped_toolchains_are_refused` and
  `test_allowlist_is_single_sourced` sets.
- Docs: update the "What is not confined" bullet in `SECURITY.md`, and the toolchain docstring.

## Installed on this machine
- Rust: rustup 1.29.1, toolchain `stable-x86_64-pc-windows-msvc`, cargo 1.99.0 under
  `%USERPROFILE%\.cargo\bin` (rustup proxies) and `%USERPROFILE%\.rustup\toolchains\<name>`.
  MSVC linker: VS 2022 Build Tools at `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools`.
- .NET SDK 8.0.425 at `C:\Program Files\dotnet`.
- Go 1.27 at `C:\Program Files\Go` (already confined).
- Add these to PATH in a shell that predates the installs.

## cargo: design notes / risks
- Snapshot the real toolchain dir (`rustup which cargo` -> `...\toolchains\<name>\bin\cargo.exe`),
  not the `~/.cargo/bin` proxy (the proxy needs RUSTUP_HOME and may self-update).
- Box env: `CARGO_HOME`, `CARGO_TARGET_DIR` via scratch_env; `CARGO_NET_OFFLINE=true`;
  `RUSTUP_TOOLCHAIN` unset. Dependencies only if vendored (`.cargo/config.toml` vendor) — else Cargo refuses offline.
- Biggest risk: the msvc target invokes `link.exe` from VS Build Tools and needs the Windows SDK
  libs. Those live outside the snapshot; either grant read on the VS/SDK dirs to the box (they are
  under Program Files, which AppContainers can usually read) or snapshot them (very large).
  Prove a real `cargo test` in a box before claiming confinement; if linking cannot work, keep
  cargo unconfined and document why.

## dotnet: design notes / risks
- Snapshot `C:\Program Files\dotnet` (SDK + runtimes; check size vs the 2 GiB limit — may need
  only `dotnet.exe`, `host`, `sdk\<ver>`, `shared\Microsoft.NETCore.App`, `packs`).
- Box env via scratch_env: `DOTNET_CLI_HOME`, `NUGET_PACKAGES`, `NUGET_HTTP_CACHE_PATH`,
  `MSBuildExtensionsPath`? Plus `DOTNET_CLI_TELEMETRY_OPTOUT=1`, `DOTNET_NOLOGO=1`,
  `DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1`, `MSBUILDDISABLENODEREUSE=1`, `DOTNET_CLI_UI_LANGUAGE=en`.
- Risks: MSBuild node reuse / named pipes, restore needs NuGet (offline only with a local cache
  or `--source` folder), `dotnet test` spawns testhost. Use `dotnet test --no-restore` against a
  repo whose packages are restored inside the box from a local feed, or document the limit.

## Verification bar (what "done" means)
Real-box acceptance tests like `test_confined_go.py` for each toolchain (pass + escapes denied +
host positive control), unit tests for resolution with fake builders, full backend suite from
the clean venv `C:\Users\krish\sentinel-venv` (`python -m pytest backend/tests` per folder with
`timeout`), CI green on 3.12/3.14, then push `master` to `origin`.
