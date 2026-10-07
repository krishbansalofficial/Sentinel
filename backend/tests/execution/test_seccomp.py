"""The seccomp program, interpreted instruction by instruction (runs on every OS)."""

from __future__ import annotations

import errno
import struct

import pytest

from backend.app.execution import seccomp
from backend.app.execution.seccomp import (
    AUDIT_ARCH,
    CLONE_NEW_NAMESPACES,
    DENIED_SYSCALLS,
    SECCOMP_RET_ALLOW,
    SECCOMP_RET_ERRNO,
    SYSCALLS,
    build_filter,
    run_filter,
)

EPERM = SECCOMP_RET_ERRNO | errno.EPERM
ENOSYS = SECCOMP_RET_ERRNO | errno.ENOSYS
ARCHES = sorted(SYSCALLS)
# A few syscalls every program needs (x86_64 / aarch64 numbers).
ALLOWED = {"x86_64": {"read": 0, "write": 1, "openat": 257, "execve": 59, "exit_group": 231,
                      "fork": 57, "socket": 41, "kill": 62},
           "aarch64": {"read": 63, "write": 64, "openat": 56, "execve": 221,
                       "exit_group": 94, "socket": 198, "kill": 129}}


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("name", DENIED_SYSCALLS)
def test_every_denied_syscall_gets_eperm(arch: str, name: str) -> None:
    program = build_filter(arch)
    assert run_filter(program, arch=AUDIT_ARCH[arch], nr=SYSCALLS[arch][name]) == EPERM


@pytest.mark.parametrize("arch", ARCHES)
def test_ordinary_syscalls_are_allowed(arch: str) -> None:
    program = build_filter(arch)
    for name, number in ALLOWED[arch].items():
        assert run_filter(program, arch=AUDIT_ARCH[arch], nr=number) == SECCOMP_RET_ALLOW, name


@pytest.mark.parametrize("arch", ARCHES)
def test_every_syscall_number_outside_the_table_is_allowed(arch: str) -> None:
    program = build_filter(arch)
    special = set(SYSCALLS[arch].values())
    for number in range(0, 460):
        if number in special:
            continue
        assert run_filter(program, arch=AUDIT_ARCH[arch], nr=number) == SECCOMP_RET_ALLOW, number


@pytest.mark.parametrize("arch", ARCHES)
@pytest.mark.parametrize("flag", [0x00020000, 0x02000000, 0x04000000, 0x08000000,
                                  0x10000000, 0x20000000, 0x40000000, 0x00000080])
def test_clone_with_any_namespace_flag_is_denied(arch: str, flag: int) -> None:
    program = build_filter(arch)
    clone = SYSCALLS[arch]["clone"]
    assert run_filter(program, arch=AUDIT_ARCH[arch], nr=clone, arg0=flag | 0x11) == EPERM
    # High (unrelated) bits in the 64-bit argument never hide a flag in the low word.
    assert run_filter(program, arch=AUDIT_ARCH[arch], nr=clone,
                      arg0=(0xFFFF << 32) | flag) == EPERM


@pytest.mark.parametrize("arch", ARCHES)
def test_plain_clone_for_threads_and_fork_is_allowed(arch: str) -> None:
    program = build_filter(arch)
    clone = SYSCALLS[arch]["clone"]
    thread_flags = 0x003D0F00  # what glibc passes for pthread_create
    fork_flags = 0x01200011  # CLONE_CHILD_SETTID|CLONE_CHILD_CLEARTID|SIGCHLD
    for flags in (thread_flags, fork_flags, 0):
        assert flags & CLONE_NEW_NAMESPACES == 0
        assert run_filter(program, arch=AUDIT_ARCH[arch], nr=clone, arg0=flags) == SECCOMP_RET_ALLOW


@pytest.mark.parametrize("arch", ARCHES)
def test_clone3_returns_enosys_so_libc_falls_back(arch: str) -> None:
    assert run_filter(build_filter(arch), arch=AUDIT_ARCH[arch],
                      nr=SYSCALLS[arch]["clone3"]) == ENOSYS


@pytest.mark.parametrize(("arch", "foreign"), [
    (arch, foreign) for arch in ARCHES
    for foreign in (0x40000003, 0xC000003E, 0xC00000B7, 0)  # i386, x86_64, aarch64, junk
    if foreign != AUDIT_ARCH[arch]])  # an architecture is never foreign to itself
def test_a_foreign_architecture_is_refused_for_every_syscall(arch: str, foreign: int) -> None:
    program = build_filter(arch)
    for number in (0, 1, 59, 101, 165):
        assert run_filter(program, arch=foreign, nr=number) == EPERM


def test_x32_abi_numbers_are_refused_on_x86_64() -> None:
    program = build_filter("x86_64")
    for number in (0, 1, 59, 512, 0x3FFFFFFF):
        expected = SECCOMP_RET_ALLOW if number < 0x40000000 and number not in (101,) else EPERM
        assert run_filter(program, arch=AUDIT_ARCH["x86_64"], nr=number) == expected
    for number in (0x40000000, 0x40000000 + 101, 0x40000000 + 165, 0xFFFFFFFF):
        assert run_filter(program, arch=AUDIT_ARCH["x86_64"], nr=number) == EPERM


def test_program_layout_is_well_formed() -> None:
    for arch in ARCHES:
        program = build_filter(arch)
        assert len(program) % 8 == 0 and 0 < len(program) // 8 < 4096
        for offset in range(0, len(program), 8):
            code, jt, jf, _k = struct.unpack_from("<HBBI", program, offset)
            target = offset // 8 + 1
            if code & 0x07 == seccomp.BPF_JMP:
                assert target + jt < len(program) // 8 and target + jf < len(program) // 8


@pytest.mark.parametrize(("machine", "expected"), [("x86_64", "x86_64"), ("AMD64", "x86_64"),
                                                   ("aarch64", "aarch64"), ("arm64", "aarch64")])
def test_host_arch_aliases(machine: str, expected: str) -> None:
    assert seccomp.host_arch(machine) == expected


@pytest.mark.parametrize("machine", ["riscv64", "ppc64le", "i686", "s390x"])
def test_unknown_architectures_fail_closed(machine: str) -> None:
    with pytest.raises(seccomp.SeccompUnsupported):
        seccomp.host_arch(machine)
