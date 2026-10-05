"""A seccomp-BPF deny filter for the Linux agent sandbox, compiled without libseccomp.

`bwrap --seccomp FD` installs a classic BPF program (an array of
``struct sock_filter``) just before it executes the sandboxed command, with
``no_new_privs`` set, so the filter is inherited by every descendant and cannot
be removed. This module builds that program. Pure module: no process starts.

What the program does, in order:

1. Loads ``seccomp_data.arch``. Any architecture other than the host's (the
   i386 ABI reached through ``int 0x80`` on x86_64, for example) gets
   ``EPERM`` for every syscall, so a foreign ABI cannot bypass the table.
2. Loads ``seccomp_data.nr``. On x86_64, numbers with the x32 bit
   (``0x40000000``) set get ``EPERM`` for the same reason.
3. ``clone`` is allowed only when its flags (argument 0, low 32 bits) request no
   new namespace. ``clone3`` passes its flags behind a pointer the filter cannot
   read, so it returns ``ENOSYS`` and libc falls back to ``clone``.
4. Each syscall in :data:`DENIED_SYSCALLS` returns ``EPERM``: tracing, mounting,
   new namespaces, kernel keyrings, BPF, perf, module and kexec loading, and
   cross-process memory access.
5. Everything else is allowed.

The verification side (`linux_sandbox`) does not trust this module: it reads
``Seccomp: 2`` and a ``Seccomp_filters`` count above the supervisor's own from
``/proc/<pid>/status`` of the live sandboxed process before releasing it.
"""

from __future__ import annotations

import errno
import platform as _platform
import struct
from collections.abc import Iterable

# linux/filter.h
BPF_LD, BPF_JMP, BPF_RET = 0x00, 0x05, 0x06
BPF_W, BPF_ABS = 0x00, 0x20
BPF_JEQ, BPF_JGE, BPF_JSET = 0x10, 0x30, 0x40
BPF_K = 0x00
# linux/seccomp.h
SECCOMP_RET_KILL_PROCESS = 0x80000000
SECCOMP_RET_ERRNO = 0x00050000
SECCOMP_RET_ALLOW = 0x7FFF0000
# struct seccomp_data offsets
_OFFSET_NR = 0
_OFFSET_ARCH = 4
_OFFSET_ARG0_LOW = 16  # args[0], little-endian low word
# linux/audit.h
AUDIT_ARCH_X86_64 = 0xC000003E
AUDIT_ARCH_AARCH64 = 0xC00000B7
X32_SYSCALL_BIT = 0x40000000
# linux/sched.h: every CLONE_NEW* flag
CLONE_NEW_NAMESPACES = (0x00020000  # CLONE_NEWNS
                        | 0x02000000  # CLONE_NEWCGROUP
                        | 0x04000000  # CLONE_NEWUTS
                        | 0x08000000  # CLONE_NEWIPC
                        | 0x10000000  # CLONE_NEWUSER
                        | 0x20000000  # CLONE_NEWPID
                        | 0x40000000  # CLONE_NEWNET
                        | 0x00000080)  # CLONE_NEWTIME

DENIED_SYSCALLS: tuple[str, ...] = (
    "ptrace", "process_vm_readv", "process_vm_writev",
    "mount", "umount2", "pivot_root", "move_mount", "open_tree", "fsopen", "fsconfig",
    "fsmount", "fspick", "mount_setattr",
    "unshare", "setns",
    "keyctl", "add_key", "request_key",
    "bpf", "perf_event_open", "userfaultfd",
    "init_module", "finit_module", "delete_module", "kexec_load", "kexec_file_load",
    "open_by_handle_at", "name_to_handle_at",
    "acct", "swapon", "swapoff", "reboot",
)

# Syscall numbers (asm/unistd_64.h for x86_64, asm-generic/unistd.h for aarch64).
SYSCALLS: dict[str, dict[str, int]] = {
    "x86_64": {
        "ptrace": 101, "process_vm_readv": 310, "process_vm_writev": 311,
        "mount": 165, "umount2": 166, "pivot_root": 155, "move_mount": 429, "open_tree": 428,
        "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442,
        "unshare": 272, "setns": 308,
        "keyctl": 250, "add_key": 248, "request_key": 249,
        "bpf": 321, "perf_event_open": 298, "userfaultfd": 323,
        "init_module": 175, "finit_module": 313, "delete_module": 176, "kexec_load": 246,
        "kexec_file_load": 320, "open_by_handle_at": 304, "name_to_handle_at": 303,
        "acct": 163, "swapon": 167, "swapoff": 168, "reboot": 169,
        "clone": 56, "clone3": 435,
    },
    "aarch64": {
        "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271,
        "mount": 40, "umount2": 39, "pivot_root": 41, "move_mount": 429, "open_tree": 428,
        "fsopen": 430, "fsconfig": 431, "fsmount": 432, "fspick": 433, "mount_setattr": 442,
        "unshare": 97, "setns": 268,
        "keyctl": 219, "add_key": 217, "request_key": 218,
        "bpf": 280, "perf_event_open": 241, "userfaultfd": 282,
        "init_module": 105, "finit_module": 273, "delete_module": 106, "kexec_load": 104,
        "kexec_file_load": 294, "open_by_handle_at": 265, "name_to_handle_at": 264,
        "acct": 89, "swapon": 224, "swapoff": 225, "reboot": 142,
        "clone": 220, "clone3": 435,
    },
}
AUDIT_ARCH = {"x86_64": AUDIT_ARCH_X86_64, "aarch64": AUDIT_ARCH_AARCH64}
_MACHINE_ALIASES = {"amd64": "x86_64", "arm64": "aarch64"}


class SeccompUnsupported(ValueError):
    """No filter table exists for this machine architecture."""


def host_arch(machine: str | None = None) -> str:
    raw = (machine or _platform.machine()).lower()
    arch = _MACHINE_ALIASES.get(raw, raw)
    if arch not in SYSCALLS:
        raise SeccompUnsupported(f"No seccomp table for architecture {raw!r}.")
    return arch


def _stmt(code: int, k: int) -> tuple[int, int, int, int]:
    return code, 0, 0, k


def _jump(code: int, k: int, jt: int, jf: int) -> tuple[int, int, int, int]:
    if not (0 <= jt <= 255 and 0 <= jf <= 255):
        raise ValueError("BPF jump offset out of range")
    return code, jt, jf, k


def build_filter(arch: str | None = None, denied: Iterable[str] = DENIED_SYSCALLS) -> bytes:
    """The BPF program for ``arch`` (default: this host) as raw ``sock_filter`` bytes."""

    resolved = host_arch(arch) if arch is None or arch not in SYSCALLS else arch
    table = SYSCALLS[resolved]
    numbers = sorted({table[name] for name in denied})
    errno_eperm = SECCOMP_RET_ERRNO | errno.EPERM
    errno_enosys = SECCOMP_RET_ERRNO | errno.ENOSYS

    program: list[tuple[int, int, int, int]] = []
    # 1. architecture: anything else is refused outright.
    program.append(_stmt(BPF_LD | BPF_W | BPF_ABS, _OFFSET_ARCH))
    program.append(_jump(BPF_JMP | BPF_JEQ | BPF_K, AUDIT_ARCH[resolved], 1, 0))
    program.append(_stmt(BPF_RET | BPF_K, errno_eperm))
    # 2. syscall number (and the x32 ABI on x86_64).
    program.append(_stmt(BPF_LD | BPF_W | BPF_ABS, _OFFSET_NR))
    if resolved == "x86_64":
        program.append(_jump(BPF_JMP | BPF_JGE | BPF_K, X32_SYSCALL_BIT, 0, 1))
        program.append(_stmt(BPF_RET | BPF_K, errno_eperm))
    # 3. clone3 -> ENOSYS; clone with namespace flags -> EPERM.
    program.append(_jump(BPF_JMP | BPF_JEQ | BPF_K, table["clone3"], 0, 1))
    program.append(_stmt(BPF_RET | BPF_K, errno_enosys))
    program.append(_jump(BPF_JMP | BPF_JEQ | BPF_K, table["clone"], 0, 4))
    program.append(_stmt(BPF_LD | BPF_W | BPF_ABS, _OFFSET_ARG0_LOW))
    program.append(_jump(BPF_JMP | BPF_JSET | BPF_K, CLONE_NEW_NAMESPACES, 0, 1))
    program.append(_stmt(BPF_RET | BPF_K, errno_eperm))
    program.append(_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW))
    # 4. the deny table: each match jumps to the shared EPERM return at the end.
    for index, number in enumerate(numbers):
        remaining = len(numbers) - index - 1
        program.append(_jump(BPF_JMP | BPF_JEQ | BPF_K, number, remaining + 1, 0))
    # 5. default allow, then the shared EPERM target.
    program.append(_stmt(BPF_RET | BPF_K, SECCOMP_RET_ALLOW))
    program.append(_stmt(BPF_RET | BPF_K, errno_eperm))
    if len(program) > 4096:  # BPF_MAXINSNS
        raise ValueError("seccomp program too long")
    return b"".join(struct.pack("<HBBI", *instruction) for instruction in program)


def run_filter(program: bytes, *, arch: int, nr: int, arg0: int = 0) -> int:
    """Interpret ``program`` for one syscall (for tests): the SECCOMP_RET_* value."""

    instructions = [struct.unpack_from("<HBBI", program, offset)
                    for offset in range(0, len(program), 8)]
    data = struct.pack("<IIQ6Q", nr & 0xFFFFFFFF, arch, 0, arg0 & 0xFFFFFFFFFFFFFFFF, 0, 0, 0, 0, 0)
    accumulator = 0
    pc = 0
    while pc < len(instructions):
        code, jt, jf, k = instructions[pc]
        if code == BPF_LD | BPF_W | BPF_ABS:
            accumulator = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code & 0x07 == BPF_JMP:
            op = code & 0xF0
            taken = ((op == BPF_JEQ and accumulator == k) or (op == BPF_JGE and accumulator >= k)
                     or (op == BPF_JSET and accumulator & k))
            pc += 1 + (jt if taken else jf)
        elif code == BPF_RET | BPF_K:
            return k
        else:
            raise ValueError(f"unsupported BPF instruction {code:#x}")
    raise ValueError("BPF program fell off the end")
