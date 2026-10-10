"""Linux confined check boxes with a fake sandbox layer (no bwrap, no cgroup).

The real ``CheckBoxes``/``CheckBox`` code runs over ``LinuxBoxHost``; only the
sandbox spawn is a host child carrying fake ``LinuxSandboxFacts``. Containment
itself is proven by ``backend/tests/execution/linux/test_linux_check_boxes.py``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from backend.app.contracts.models import JournalEventType
from backend.app.core.database import Database
from backend.app.core.errors import AppError
from backend.app.core.journal import JournalWriter
from backend.app.execution.check_box import (
    BoxRuntime,
    CheckBoxes,
    CheckRunFacts,
    ReadonlyBind,
    verified_boundary,
)
from backend.app.execution.check_repository import (
    CheckRunEvent,
    CheckRunRecord,
    CheckRunRepository,
    CheckRunState,
    change_check_runs_fact,
    check_run_events,
    check_run_fact,
    record_boundary,
)
from backend.app.execution.linux_check_box import (
    LinuxBoxHost,
    LinuxToolFinders,
    linux_identity,
    linux_resolve_check_runtime,
    remove_tree,
    runtime_bind,
)
from backend.app.execution.linux_sandbox import NAMESPACES, LinuxSandboxFacts
from backend.tests.execution.test_check_box import _make_change
from backend.tests.support_kb import make_repo

POSIX = os.name != "nt"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
WINDOWS_SID = "S-1-15-2-1111111111-2222222222-3333333333-444444444-555555555-666666666-777777777"


def linux_facts(*, network: bool = False, **overrides) -> LinuxSandboxFacts:
    values = dict(
        pid=4242, namespaces={name: f"{name}:[4026532{index}]" for index, name in enumerate(NAMESPACES)},
        host_namespaces={name: f"{name}:[4026531{index}]" for index, name in enumerate(NAMESPACES)},
        seccomp_mode="2", seccomp_filters=2, supervisor_seccomp_filters=0, no_new_privs="1",
        uid_map=("0 1000 1",), cgroup="/sentinel/sentinel-run-" + "a" * 32,
        expected_cgroup="/sentinel/sentinel-run-" + "a" * 32, network_isolated=not network,
        verified=True,
    )
    if network:
        values["namespaces"] = {**values["namespaces"], "net": values["host_namespaces"]["net"]}
    values.update(overrides)
    return LinuxSandboxFacts(**values)


# ---------------------------------------------------------------------- fakes


class FakeHierarchy:
    def __init__(self) -> None:
        self.prepared = 0
        self.runs: list[UUID] = []

    def prepare(self):
        self.prepared += 1
        return ()

    def create_run(self, run_id, limits):
        self.runs.append(run_id)
        return SimpleNamespace(run_id=run_id)


class FakeSandbox:
    """Records each SandboxSpec and starts it as a plain host child with fake facts."""

    def __init__(self, facts: LinuxSandboxFacts | None = None) -> None:
        self.specs = []
        self.facts = facts or linux_facts()

    def __call__(self, spec, cgroup, *, redact):
        self.specs.append(spec)
        env = dict(spec.env)
        if os.name == "nt":  # a Windows host child needs these to start at all
            env.update({key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR")
                        if key in os.environ})
        process = subprocess.Popen(list(spec.argv), cwd=spec.cwd, env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0)
        process.linux_sandbox = replace(self.facts, network_isolated=not spec.network)
        if spec.network:
            process.linux_sandbox = linux_facts(network=True)
        return process


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "checks.sqlite3")
    database.initialize()
    return database


def _host(tmp_path: Path, **kwargs) -> tuple[LinuxBoxHost, FakeHierarchy, FakeSandbox, list]:
    hierarchy = FakeHierarchy()
    sandbox = kwargs.pop("sandbox", FakeSandbox())
    calls: list[str] = []
    host = LinuxBoxHost(tmp_path / "store", cgroups=lambda: hierarchy, spawn_sandbox=sandbox,
                        require=kwargs.pop("require", lambda: calls.append("require")),
                        **kwargs)
    return host, hierarchy, sandbox, calls


def _boxes(tmp_path, database, host) -> CheckBoxes:
    return CheckBoxes(database, journal=JournalWriter(database), profile_prefix="sentinel.test.",
                      platform=host.platform())


def _events(database: Database, change_id: UUID) -> list[dict]:
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT event_type, payload_json FROM journal_events WHERE change_id = ? "
            "ORDER BY seq", (str(change_id),)).fetchall()
    return [{"type": row["event_type"], "payload": json.loads(row["payload_json"])}
            for row in rows]


PYTHON_RUNTIME = BoxRuntime(executable=Path(sys.executable))


# ---------------------------------------------------------------------- platform


def test_identity_is_the_linux_prefix_and_never_a_sid(tmp_path) -> None:
    host, *_ = _host(tmp_path)
    assert host.derive_package_sid("sentinel.check.abc") == "linux-sandbox:sentinel.check.abc"
    assert host.container_folder("linux-sandbox:sentinel.check.abc") == (
        tmp_path / "store" / "linux-checks" / "Packages" / "sentinel.check.abc" / "AC")
    with pytest.raises(ValueError):
        host.container_folder(WINDOWS_SID)
    with pytest.raises(ValueError):
        host.derive_package_sid("Bad/Name")


def test_ensure_profile_creates_private_storage(tmp_path) -> None:
    host, *_ = _host(tmp_path)
    profile, created = host.ensure_profile("sentinel.test.one")
    assert created is True
    assert profile.package_sid == linux_identity("sentinel.test.one")
    assert profile.container_path.is_dir()
    assert host.profile_exists("sentinel.test.one")
    assert host.ensure_profile("sentinel.test.one")[1] is False
    if POSIX:
        for path in (host.root, profile.container_path, profile.container_path.parent):
            assert stat.S_IMODE(os.lstat(path).st_mode) == 0o700


@pytest.mark.skipif(not POSIX, reason="symlink refusal is exercised on POSIX")
def test_ensure_profile_refuses_a_symlinked_storage_folder(tmp_path) -> None:
    host, *_ = _host(tmp_path)
    packages = host.root / "Packages"
    packages.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (packages / "sentinel.test.link").symlink_to(elsewhere)
    with pytest.raises(OSError):
        host.ensure_profile("sentinel.test.link")


def test_base_environment_copies_no_host_variable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SENTINEL_SECRET_CANARY", "leak")
    host, *_ = _host(tmp_path)
    profile, _ = host.ensure_profile("sentinel.test.env")
    (profile.container_path / "scratch").mkdir()
    env = host.base_environment(profile.container_path, path_entries=["/opt/tool/bin"])
    assert set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}
    assert env["PATH"] == "/opt/tool/bin:/usr/local/bin:/usr/bin:/bin"
    assert Path(env["HOME"]).parent == profile.container_path / "scratch"
    assert Path(env["TMPDIR"]).is_dir()


def test_spawn_binds_only_the_tree_and_scratch_writable(tmp_path) -> None:
    host, hierarchy, sandbox, calls = _host(tmp_path)
    profile, _ = host.ensure_profile("sentinel.test.spawn")
    tree = profile.container_path / "tree"
    tree.mkdir()
    process = host.spawn(
        [sys.executable, "-c", "pass"], cwd=tree, env={"PATH": "/usr/bin"},
        redact=lambda text: text, profile_name="sentinel.test.spawn",
        expected_package_sid=profile.package_sid, capabilities=(),
        readonly_binds=[(tmp_path / "runtime", tree / "node_modules")])
    process.wait(30)
    spec = sandbox.specs[0]
    assert spec.writable == (tree, profile.container_path / "scratch")
    assert spec.mounts == ((tmp_path / "runtime", tree / "node_modules"),)
    assert spec.network is False and spec.cwd == tree
    assert calls == ["require"] and hierarchy.prepared == 1 and len(hierarchy.runs) == 1


def test_spawn_shares_network_only_for_a_declared_capability(tmp_path) -> None:
    host, _, sandbox, _ = _host(tmp_path)
    profile, _ = host.ensure_profile("sentinel.test.net")
    (profile.container_path / "tree").mkdir()
    host.spawn([sys.executable, "-c", "pass"], cwd=profile.container_path / "tree", env={},
               redact=str, profile_name="sentinel.test.net",
               expected_package_sid=profile.package_sid,
               capabilities=("internetClient",)).wait(30)
    assert sandbox.specs[0].network is True


@pytest.mark.parametrize("problem", ["identity", "cwd"])
def test_spawn_refuses_a_mismatched_identity_or_cwd(tmp_path, problem) -> None:
    host, hierarchy, sandbox, _ = _host(tmp_path)
    profile, _ = host.ensure_profile("sentinel.test.bad")
    with pytest.raises(AppError) as raised:
        host.spawn([sys.executable], redact=str, env={}, capabilities=(),
                   profile_name="sentinel.test.bad",
                   cwd=profile.container_path / ("tree" if problem == "identity" else "scratch"),
                   expected_package_sid=(WINDOWS_SID if problem == "identity"
                                         else profile.package_sid))
    assert raised.value.code == "LINUX_SANDBOX_VERIFICATION_FAILED"
    assert sandbox.specs == [] and hierarchy.runs == []


def test_spawn_fails_closed_before_any_cgroup_without_a_sandbox(tmp_path) -> None:
    def missing():
        raise AppError("LINUX_SANDBOX_UNAVAILABLE", "no bwrap", status_code=501)

    host, hierarchy, sandbox, _ = _host(tmp_path, require=missing)
    profile, _ = host.ensure_profile("sentinel.test.nobwrap")
    with pytest.raises(AppError) as raised:
        host.spawn([sys.executable], cwd=profile.container_path / "tree", env={}, redact=str,
                   profile_name="sentinel.test.nobwrap",
                   expected_package_sid=profile.package_sid)
    assert raised.value.code == "LINUX_SANDBOX_UNAVAILABLE"
    assert hierarchy.runs == [] and hierarchy.prepared == 0 and sandbox.specs == []


@pytest.mark.skipif(not POSIX, reason="POSIX modes and symlinks")
def test_remove_tree_repairs_modes_and_never_follows_links(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    box = tmp_path / "box"
    (box / "locked" / "deep").mkdir(parents=True)
    (box / "locked" / "deep" / "file.txt").write_text("x")
    (box / "link").symlink_to(outside)
    (box / "readonly").mkdir()
    (box / "readonly" / "f").write_text("x")
    os.chmod(box / "readonly", 0o500)
    os.chmod(box / "locked" / "deep", 0o000)
    os.chmod(box / "locked", 0o000)
    remove_tree(box)
    assert not os.path.lexists(box)
    assert (outside / "keep.txt").read_text() == "keep"
    remove_tree(box)  # idempotent


def test_the_platform_has_no_runtime_cache(tmp_path, database) -> None:
    host, *_ = _host(tmp_path)
    report = _boxes(tmp_path, database, host).sweep_runtime_cache()
    assert not report.removed_partials and not report.evicted and not report.failed


# ---------------------------------------------------------------------- end to end


def test_a_box_run_is_journaled_as_linux_sandbox(tmp_path, database) -> None:
    host, _, sandbox, _ = _host(tmp_path)
    boxes = _boxes(tmp_path, database, host)
    change_id = _make_change(database)
    repo = make_repo(tmp_path / "repo", {"app.py": "VALUE = 1\n"})
    with boxes.open(change_id, repo, PYTHON_RUNTIME) as box:
        assert box.package_sid.startswith("linux-sandbox:")
        facts = box.run([sys.executable, "-c", "print(open('app.py').read().strip())"],
                        timeout=60, limit=4096)
        container = box.container
    assert facts.exit_code == 0 and facts.stdout.strip() == b"VALUE = 1"
    assert facts.boundary == "LINUX_SANDBOX" and facts.appcontainer is None
    assert verified_boundary(facts) == "LINUX_SANDBOX"
    record = boxes.repository.get(facts.check_run_id)
    assert record.state is CheckRunState.CLEANED
    assert record.facts["sandbox_kind"] == "linux_sandbox"
    assert not os.path.lexists(container.parent)
    events = [event for event in _events(database, change_id)
              if event["type"] == "check.confined_run"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["boundary"] == "LINUX_SANDBOX"
    assert payload["package_sid"] == record.package_sid
    assert payload["linux_sandbox"] == record.facts
    assert "is_appcontainer" not in payload and payload["network"] is False
    assert sandbox.specs[0].env["HOME"].startswith(str(container))
    fact, reason = check_run_fact(
        facts.check_run_id, "LINUX_SANDBOX", records={record.id: record},
        events=CheckRunRepository(database).events_for_change(change_id))
    assert (fact, reason) == ("PASS", None)


def test_missing_live_facts_fail_closed_and_finish_the_row(tmp_path, database) -> None:
    sandbox = FakeSandbox()

    def spawn_without_facts(*args, **kwargs):
        process = sandbox(*args, **kwargs)
        del process.linux_sandbox
        return process

    host, *_ = _host(tmp_path, sandbox=spawn_without_facts)
    boxes = _boxes(tmp_path, database, host)
    change_id = _make_change(database)
    repo = make_repo(tmp_path / "repo")
    with boxes.open(change_id, repo, PYTHON_RUNTIME) as box:
        with pytest.raises(AppError) as raised:
            box.run([sys.executable, "-c", "pass"], timeout=60, limit=1024)
        assert raised.value.code == "APPCONTAINER_VERIFICATION_FAILED"
        assert boxes.repository.get(box.id).state is CheckRunState.FINISHED
    assert not any(event["type"] == "check.confined_run"
                   for event in _events(database, change_id))


def test_unverified_live_facts_are_never_claimed(tmp_path, database) -> None:
    sandbox = FakeSandbox(linux_facts(verified=False, failures=("seccomp mode is 0",)))
    host, *_ = _host(tmp_path, sandbox=sandbox)
    boxes = _boxes(tmp_path, database, host)
    change_id = _make_change(database)
    repo = make_repo(tmp_path / "repo")
    with boxes.open(change_id, repo, PYTHON_RUNTIME) as box:
        facts = box.run([sys.executable, "-c", "pass"], timeout=60, limit=1024)
    assert verified_boundary(facts) is None
    records = {record.id: record for record in CheckRunRepository(database).for_change(change_id)}
    fact, _ = check_run_fact(facts.check_run_id, "LINUX_SANDBOX", records=records,
                             events=CheckRunRepository(database).events_for_change(change_id))
    assert fact == "FAIL"


def test_a_tree_bind_destination_that_is_not_a_folder_is_refused(tmp_path, database) -> None:
    host, _, sandbox, _ = _host(tmp_path)
    boxes = _boxes(tmp_path, database, host)
    change_id = _make_change(database)
    repo = make_repo(tmp_path / "repo", {"node_modules": "a file, not a folder\n"})
    runtime = BoxRuntime(executable=Path(sys.executable),
                         readonly_binds=(ReadonlyBind(tmp_path, "node_modules"),))
    with boxes.open(change_id, repo, runtime) as box:
        with pytest.raises(AppError) as raised:
            box.run([sys.executable, "-c", "pass"], timeout=30, limit=1024)
    assert raised.value.code == "CHECK_TREE_REPARSE_POINT"
    assert sandbox.specs == []


def test_readonly_bind_validation() -> None:
    with pytest.raises(ValueError):
        ReadonlyBind(Path("relative"))
    for name in ("", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            ReadonlyBind(Path(sys.executable).parent, name)
    with pytest.raises(ValueError):
        BoxRuntime(env={"HOME": "/x"})


# ---------------------------------------------------------------------- facts


def _linux_record(run_id: UUID, *, facts, network: bool = False,
                  identity: str | None = None) -> CheckRunRecord:
    return CheckRunRecord(
        id=run_id, change_id=uuid4(), profile_name=f"sentinel.check.{run_id.hex}",
        package_sid=identity or linux_identity(f"sentinel.check.{run_id.hex}"),
        state=CheckRunState.CLEANED, network=network, runtime_grants=(), created_at=NOW,
        updated_at=NOW, tree_digest="a" * 64, facts=facts, exit_code=0, timed_out=False)


def _linux_event(record: CheckRunRecord, **overrides) -> CheckRunEvent:
    payload = {"check_run_id": str(record.id), "package_sid": record.package_sid,
               "boundary": "LINUX_SANDBOX", "linux_sandbox": record.facts,
               "network": record.network, **overrides}
    return CheckRunEvent("check.confined_run", str(record.id), payload)


def _token():
    return {"profile_name": "x", "package_sid": WINDOWS_SID, "is_appcontainer": True,
            "integrity_rid": "0x1000", "capability_sids": [], "job_verified": True,
            "verified_at": "2026-10-10T12:00:00+00:00"}


def test_a_verified_linux_run_passes() -> None:
    record = _linux_record(uuid4(), facts=linux_facts().to_payload())
    assert record_boundary(record) == "LINUX_SANDBOX"
    assert check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: record},
                          events=[_linux_event(record)]) == ("PASS", None)
    fact, reason, runs = change_check_runs_fact(records={record.id: record},
                                                events=[_linux_event(record)])
    assert (fact, runs) == ("PASS", [(record.id, "LINUX_SANDBOX")])


def test_an_appcontainer_claim_on_a_linux_box_is_refused() -> None:
    record = _linux_record(uuid4(), facts=linux_facts().to_payload())
    fact, reason = check_run_fact(record.id, "APPCONTAINER", records={record.id: record},
                                  events=[_linux_event(record)])
    assert fact == "FAIL" and "not the boundary of the check box" in reason


def test_a_linux_claim_on_an_appcontainer_box_is_refused() -> None:
    record = _linux_record(uuid4(), facts=linux_facts().to_payload(), identity=WINDOWS_SID)
    assert record_boundary(record) == "APPCONTAINER"
    fact, _ = check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: record},
                             events=[_linux_event(record)])
    assert fact == "FAIL"
    # Linux facts never satisfy the AppContainer verifier either.
    fact, _ = check_run_fact(record.id, "APPCONTAINER", records={record.id: record},
                             events=[_linux_event(record, boundary="APPCONTAINER")])
    assert fact == "FAIL"


def test_token_facts_on_a_linux_box_do_not_verify() -> None:
    record = _linux_record(uuid4(), facts=_token())
    fact, _ = check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: record},
                             events=[_linux_event(record)])
    assert fact == "FAIL"


@pytest.mark.parametrize("tamper", [
    {"seccomp_filters": 0},
    {"seccomp_mode": "0"},
    {"no_new_privs": "0"},
    {"cgroup": "/elsewhere"},
    {"failures": ["uid_map does not map exactly this user"]},
    {"verified": False},
    {"sandbox_kind": "something_else"},
    {"network_isolated": False},  # the box declared no network
])
def test_tampered_row_facts_fail(tamper) -> None:
    facts = {**linux_facts().to_payload(), **tamper}
    record = _linux_record(uuid4(), facts=facts)
    honest = replace(record, facts=linux_facts().to_payload())
    fact, _ = check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: record},
                             events=[_linux_event(honest)])
    assert fact == "FAIL"


def test_a_namespace_equal_to_the_host_fails() -> None:
    facts = linux_facts().to_payload()
    facts["namespaces"]["mnt"] = facts["host_namespaces"]["mnt"]
    record = _linux_record(uuid4(), facts=facts)
    assert check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: record},
                          events=[_linux_event(record)])[0] == "FAIL"


@pytest.mark.parametrize("override", [
    {"boundary": "APPCONTAINER"},
    {"package_sid": "linux-sandbox:sentinel.check.other"},
    {"linux_sandbox": None},
    {"linux_sandbox": {**linux_facts().to_payload(), "seccomp_filters": 0}},
    {"check_run_id": str(uuid4())},
])
def test_tampered_journal_payloads_fail(override) -> None:
    record = _linux_record(uuid4(), facts=linux_facts().to_payload())
    event = _linux_event(record, **override)
    if "check_run_id" in override:
        event = CheckRunEvent(event.event_type, str(record.id), event.payload)
    fact, _ = check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: record},
                             events=[_linux_event(record), event])
    assert fact == "FAIL"


def test_a_networked_linux_box_needs_network_facts() -> None:
    networked = linux_facts(network=True).to_payload()
    record = _linux_record(uuid4(), facts=networked, network=True)
    assert check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: record},
                          events=[_linux_event(record)]) == ("PASS", None)
    isolated_claim = replace(record, network=False)
    assert check_run_fact(record.id, "LINUX_SANDBOX", records={record.id: isolated_claim},
                          events=[_linux_event(isolated_claim)])[0] == "FAIL"


def test_live_verified_boundary_never_cross_accepts() -> None:
    base = dict(check_run_id=uuid4(), exit_code=0, timed_out=False, stdout=b"", stderr=b"",
                truncated=False, stdout_digest="0" * 64, capabilities=(), network=False,
                argv_sha256="0" * 64, tree_digest="0" * 64, duration_ms=1)
    good = CheckRunFacts(**base, appcontainer=None, boundary="LINUX_SANDBOX",
                         linux_sandbox=linux_facts())
    assert verified_boundary(good) == "LINUX_SANDBOX"
    assert verified_boundary(replace(good, boundary="APPCONTAINER")) is None
    assert verified_boundary(replace(good, network=True)) is None  # facts say isolated
    assert verified_boundary(replace(good, linux_sandbox=linux_facts(seccomp_filters=0))) is None
    assert verified_boundary(replace(good, linux_sandbox=None)) is None


# ---------------------------------------------------------------------- API and Passport


def _persist_linux_run(database: Database, change_id: UUID, *, facts=None) -> CheckRunRecord:
    run_id = uuid4()
    record = replace(_linux_record(run_id, facts=facts or linux_facts().to_payload()),
                     change_id=change_id)
    CheckRunRepository(database).insert(record)
    event = _linux_event(record)
    JournalWriter(database).append(change_id, JournalEventType.CHECK_CONFINED_RUN,
                                   subject_type="check_run", subject_id=run_id,
                                   payload=dict(event.payload))
    return record


def test_the_check_runs_api_reports_linux_sandbox(database) -> None:
    from backend.app.core.runtime_service import CheckRunService

    change_id = _make_change(database)
    record = _persist_linux_run(database, change_id)
    service = CheckRunService(CheckRunRepository(database),
                              SimpleNamespace(get=lambda change: None))
    [view] = service.list_for_change(change_id).items
    assert view.boundary == "LINUX_SANDBOX" and view.token is None
    assert view.linux_sandbox is not None and view.linux_sandbox.verified is True
    assert set(view.linux_sandbox.separate_namespaces) == set(NAMESPACES)
    assert view.linux_sandbox.seccomp_filters_added == 2
    assert view.linux_sandbox.network_isolated is True
    assert view.linux_sandbox.cgroup == record.facts["cgroup"]


def test_the_check_runs_api_withholds_the_boundary_for_tampered_facts(database) -> None:
    from backend.app.core.runtime_service import CheckRunService

    change_id = _make_change(database)
    _persist_linux_run(database, change_id,
                       facts={**linux_facts().to_payload(), "no_new_privs": "0"})
    service = CheckRunService(CheckRunRepository(database),
                              SimpleNamespace(get=lambda change: None))
    [view] = service.list_for_change(change_id).items
    assert view.boundary is None
    assert view.linux_sandbox is not None and view.linux_sandbox.no_new_privs is False
    assert view.linux_sandbox.verified is False


def test_passport_confined_checks_pass_for_a_linux_run(tmp_path) -> None:
    from backend.app.passport.v2 import PassportV2Issuer
    from backend.tests.passport.test_builder import _database, _seed_change

    database = _database(tmp_path)
    change = _seed_change(database)
    record = _persist_linux_run(database, change.id)
    payload = PassportV2Issuer(database).snapshot(change.id)
    assert payload.confined_checks == "PASS", payload.limitations
    assert [(item.check_run_id, item.boundary) for item in payload.check_runs] == [
        (record.id, "LINUX_SANDBOX")]


def test_passport_confined_checks_fail_for_tampered_linux_facts(tmp_path) -> None:
    from backend.app.passport.v2 import PassportV2Issuer
    from backend.tests.passport.test_builder import _database, _seed_change

    database = _database(tmp_path)
    change = _seed_change(database)
    record = _persist_linux_run(database, change.id)
    with database.connection() as connection:
        connection.execute("UPDATE check_runs SET facts_json = ? WHERE id = ?",
                           (json.dumps({**record.facts, "cgroup": "/"}), str(record.id)))
    payload = PassportV2Issuer(database).snapshot(change.id)
    assert payload.confined_checks == "FAIL"
    assert [item.boundary for item in payload.check_runs] == [None]


def test_journal_rows_parse_into_check_run_events(database) -> None:
    change_id = _make_change(database)
    _persist_linux_run(database, change_id)
    events = CheckRunRepository(database).events_for_change(change_id)
    assert [event.payload["boundary"] for event in events] == ["LINUX_SANDBOX"]
    assert check_run_events([("check.confined_run", "x", "{bad")])[0].payload is None


# ---------------------------------------------------------------------- resolver


def _home(monkeypatch, path: Path) -> None:
    """The user's home as ``Path.home()`` reports it (HOME on POSIX, USERPROFILE on Windows)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path(path)))


def _venv(tmp_path: Path) -> tuple[Path, Path]:
    base = tmp_path / "pythons" / "3.12"
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "python3.12").write_text("")
    venv = tmp_path / "work" / "venv"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text("")
    python.chmod(0o755)
    (venv / "pyvenv.cfg").write_text(f"home = {base / 'bin'}\nversion = 3.12\n")
    return venv, base


def test_python_binds_the_venv_and_its_base(tmp_path, monkeypatch) -> None:
    _home(monkeypatch, str(tmp_path / "home"))
    venv, base = _venv(tmp_path)
    resolved = linux_resolve_check_runtime("pytest", interpreter=venv / "bin" / "python")
    sources = {bind.source for bind in resolved.runtime.readonly_binds}
    assert {Path(os.path.realpath(venv)), Path(os.path.realpath(base))} <= sources
    assert resolved.argv_prefix == (str(venv / "bin" / "python"), "-m", "pytest")
    assert resolved.runtime.snapshots == ()
    assert resolved.runtime.env["PYTHONNOUSERSITE"] == "1"


def test_python_refuses_a_bind_that_exposes_home_repo_or_store(tmp_path, monkeypatch) -> None:
    venv, base = _venv(tmp_path)
    _home(monkeypatch, str(venv / "home-inside"))  # the venv would expose $HOME
    (venv / "home-inside").mkdir()
    with pytest.raises(AppError) as raised:
        linux_resolve_check_runtime("python", interpreter=venv / "bin" / "python")
    assert raised.value.code == "CHECK_RUNTIME_UNAVAILABLE"
    _home(monkeypatch, str(tmp_path / "home"))
    with pytest.raises(AppError):
        linux_resolve_check_runtime("python", interpreter=venv / "bin" / "python",
                                    source_root=venv)
    with pytest.raises(AppError):
        linux_resolve_check_runtime("python", interpreter=venv / "bin" / "python",
                                    protected=(base / "bin",))


def test_runtime_bind_rules(tmp_path) -> None:
    assert runtime_bind("/usr", executable="python") is None if POSIX else True
    with pytest.raises(AppError):
        runtime_bind(tmp_path / "missing", executable="python")
    with pytest.raises(AppError):
        runtime_bind(Path(tmp_path.anchor), executable="python")
    assert runtime_bind(tmp_path, executable="python").source == Path(os.path.realpath(tmp_path))


def test_a_relative_or_missing_interpreter_is_refused(tmp_path) -> None:
    for interpreter in ("python3", tmp_path / "nope"):
        with pytest.raises(AppError) as raised:
            linux_resolve_check_runtime("python", interpreter=interpreter)
        assert raised.value.code == "CHECK_RUNTIME_UNAVAILABLE"


@pytest.mark.parametrize("executable", ["cargo", "dotnet", "uv", "bash"])
def test_unconfined_toolchains_stay_refused(executable) -> None:
    with pytest.raises(AppError) as raised:
        linux_resolve_check_runtime(executable)
    assert raised.value.code == "CHECK_TOOLCHAIN_UNCONFINED"


def _node_install(tmp_path: Path) -> Path:
    prefix = tmp_path / "opt" / "node"
    (prefix / "bin").mkdir(parents=True)
    node = prefix / "bin" / "node"
    node.write_text("")
    cli = prefix / "lib" / "node_modules" / "npm" / "bin"
    cli.mkdir(parents=True)
    (cli / "npm-cli.js").write_text("")
    return node


@pytest.mark.parametrize("npm_directory", ["lib/node_modules", "share/nodejs"])
def test_node_binds_the_install_and_node_modules_in_the_tree(
        tmp_path, monkeypatch, npm_directory) -> None:
    _home(monkeypatch, str(tmp_path / "home"))
    node = _node_install(tmp_path)
    if npm_directory == "share/nodejs":
        old = node.parent.parent / "lib" / "node_modules" / "npm"
        target = node.parent.parent / "share" / "nodejs" / "npm"
        target.parent.mkdir(parents=True)
        old.rename(target)
    repo = tmp_path / "repo"
    (repo / "node_modules" / "pnpm" / "bin").mkdir(parents=True)
    (repo / "node_modules" / "pnpm" / "bin" / "pnpm.cjs").write_text("")
    finders = LinuxToolFinders(node=lambda root: node)
    npm = linux_resolve_check_runtime("npm", source_root=repo, finders=finders)
    assert npm.argv_prefix[1].endswith("npm-cli.js")
    binds = {(bind.source, bind.tree_relative) for bind in npm.runtime.readonly_binds}
    assert (Path(os.path.realpath(node.parent.parent)), None) in binds
    assert (repo.resolve() / "node_modules", "node_modules") in binds
    pnpm = linux_resolve_check_runtime("pnpm", source_root=repo, finders=finders)
    assert pnpm.argv_prefix[1] == "node_modules/pnpm/bin/pnpm.cjs"
    with pytest.raises(AppError):
        linux_resolve_check_runtime("yarn", source_root=repo, finders=finders)
    with pytest.raises(AppError):
        linux_resolve_check_runtime("node", finders=LinuxToolFinders(node=lambda root: None))


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_a_symlinked_node_modules_is_refused(tmp_path, monkeypatch) -> None:
    _home(monkeypatch, str(tmp_path / "home"))
    node = _node_install(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "node_modules").symlink_to(tmp_path)
    with pytest.raises(AppError) as raised:
        linux_resolve_check_runtime("npm", source_root=repo,
                                    finders=LinuxToolFinders(node=lambda root: node))
    assert raised.value.code == "CHECK_RUNTIME_UNAVAILABLE"


def test_go_binds_goroot_offline_with_caches_in_scratch(tmp_path, monkeypatch) -> None:
    _home(monkeypatch, str(tmp_path / "home"))
    goroot = tmp_path / "go"
    (goroot / "bin").mkdir(parents=True)
    (goroot / "src").mkdir()
    go = goroot / "bin" / "go"
    go.write_text("")
    resolved = linux_resolve_check_runtime("go", finders=LinuxToolFinders(go=lambda root: go))
    runtime = resolved.runtime
    assert runtime.env["GOPROXY"] == "off" and runtime.env["CGO_ENABLED"] == "0"
    assert runtime.env["GOROOT"] == str(Path(os.path.realpath(goroot)))
    assert set(runtime.scratch_env) == {"GOCACHE", "GOPATH", "GOTMPDIR"}
    assert [bind.source for bind in runtime.readonly_binds] == [Path(os.path.realpath(goroot))]
    (goroot / "src").rmdir()
    with pytest.raises(AppError):
        linux_resolve_check_runtime("go", finders=LinuxToolFinders(go=lambda root: go))


# ---------------------------------------------------------------------- composition root


def test_create_app_picks_the_linux_layer_only_on_linux(tmp_path, monkeypatch) -> None:
    import backend.app.main as main

    monkeypatch.setattr(main.sys, "platform", "linux")
    layer = main._check_box_layer(tmp_path)
    assert layer["platform"].runtime_cache is False
    assert layer["platform"].derive_package_sid("sentinel.check.x").startswith("linux-sandbox:")
    assert layer["resolver"].keywords["protected"] == (tmp_path,)
    for other in ("win32", "darwin"):
        monkeypatch.setattr(main.sys, "platform", other)
        assert main._check_box_layer(tmp_path) == {}
