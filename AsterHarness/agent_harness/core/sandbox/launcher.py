"""Trusted Linux launcher, also usable as an isolated stdio MCP executable.

Only stdlib imports: launched with Python -I -S, never from agent startup hooks.
Filesystem masks use pinned descriptors; seccomp closes host-process escape APIs.
"""

import ctypes
import errno
import json
from itertools import count
import os
from pathlib import Path
import sys
import subprocess
import signal
import stat
import tempfile


def syscall_number(lib, name):
    number = lib.seccomp_syscall_resolve_name(name.encode())
    if number < 0 and name == "mount_setattr":
        # Ubuntu 20.04's libseccomp predates this syscall, but WSL kernels do
        # implement it. Use the ABI number only for explicitly known ABIs.
        lib.seccomp_arch_native.restype = ctypes.c_uint32
        if lib.seccomp_arch_native() in (0xC000003E, 0xC00000B7, 0x40000003):
            return 442  # x86-64, AArch64, x86; not x32 or MIPS.
        raise RuntimeError("Update libseccomp to constrain mount_setattr on this ABI")
    if number < 0 and name in ("fsopen", "fsconfig", "fsmount", "fspick", "open_tree", "move_mount"):
        raise RuntimeError("Update libseccomp to constrain " + name)
    return number


def seccomp_fd(*, local_ipc=False):
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # ALLOW; targeted errno denies below.
    if not context:
        raise RuntimeError("Cannot initialize seccomp")

    class Comparison(ctypes.Structure):
        _fields_ = [
            ("arg", ctypes.c_uint),
            ("op", ctypes.c_uint),
            ("a", ctypes.c_uint64),
            ("b", ctypes.c_uint64),
        ]

    try:
        action = 0x00050000 | errno.EPERM
        for name in (
            "mount",
            "umount2",
            "pivot_root",
            # Deny the newer mount API as well: child user namespaces can
            # acquire local capabilities, but must not rearrange our mounts.
            "fsopen",
            "fsconfig",
            "fsmount",
            "fspick",
            "open_tree",
            "move_mount",
            "mount_setattr",
            "setns",
            "ptrace",
            "process_vm_readv",
            "process_vm_writev",
            "pidfd_getfd",
            "open_by_handle_at",
            "bpf",
            "perf_event_open",
            "io_uring_setup",
            "io_uring_enter",
            "io_uring_register",
            "kexec_load",
            "init_module",
            "finit_module",
            "delete_module",
        ):
            number = syscall_number(lib, name)
            if number >= 0 and lib.seccomp_rule_add(context, action, number, 0) < 0:
                raise RuntimeError("Cannot construct seccomp filter")
        # Creating an inner user namespace does not grant capabilities in the
        # enclosing namespace. Allow only this operation, not arbitrary unshare.
        number = lib.seccomp_syscall_resolve_name(b"unshare")
        comparison = Comparison(0, 1, 0x10000000, 0)  # NE CLONE_NEWUSER
        if number < 0 or lib.seccomp_rule_add(context, action, number, 1, comparison) < 0:
            raise RuntimeError("Cannot constrain nested user namespaces")
        number = lib.seccomp_syscall_resolve_name(b"socket")
        for family in ((40,) if local_ipc else (1, 40)):  # AF_UNIX opt-in; AF_VSOCK always denied.
            comparison = Comparison(0, 4, family, 0)  # SCMP_CMP_EQ
            if lib.seccomp_rule_add(context, action, number, 1, comparison) < 0:
                raise RuntimeError("Cannot constrain local socket families")
        with tempfile.TemporaryFile() as stream:
            fd = os.dup(stream.fileno())
        if lib.seccomp_export_bpf(context, fd) < 0:
            os.close(fd)
            raise RuntimeError("Cannot export seccomp filter")
        os.lseek(fd, 0, 0)
        return fd
    finally:
        lib.seccomp_release(context)


def checked_path(value):
    path = Path(value)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise ValueError(
            "Sandbox paths must exist and be canonical absolute paths: " + str(path)
        )
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError(
                "Sandbox mount paths cannot contain symlinks: " + str(path)
            )
        current = current.parent
    return path


def pin_path(path):
    """Walk pinned parent descriptors, rejecting symlinks at every component."""
    fd = os.open('/', os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            flags = os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC
            if index < len(path.parts) - 2:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            if stat.S_ISLNK(os.fstat(fd).st_mode):
                raise ValueError('Sandbox source cannot be a symlink')
        return fd
    except BaseException:
        os.close(fd)
        raise


def launch(config, argv):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Filesystem sandbox requires Linux/WSL2")
    if not argv:
        raise ValueError("Missing sandbox command")
    descriptors = []

    def pinned(path):
        fd = pin_path(path)
        descriptors.append(fd)
        os.set_inheritable(fd, True)
        return str(fd)

    root_write = any(
        e["path"] == "/" and e["access"] == "write" for e in config["mounts"]
    )
    args = [
        "/usr/bin/bwrap",
        "--die-with-parent",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--bind-fd" if root_write else "--ro-bind-fd",
        pinned(Path("/")),
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/run",
        "--dir",
        "/run/aster-harness",
    ]
    with tempfile.TemporaryDirectory(prefix="aster-masks-") as masks:
        policy = [*config["mounts"], *[{"path": p, "access": "deny"} for p in config["denied"]]]
        resolver = checked_path(config["resolver"]) if config.get("resolver") else None
        resolver_target = config.get("resolver_target")
        if resolver_target is not None and (resolver is None or not Path(resolver_target).is_absolute()
                                            or '..' in Path(resolver_target).parts
                                            or Path(resolver_target).name != 'resolv.conf'):
            raise ValueError("Invalid resolver mount target")

        mask_ids = count()
        def mask(path, openings=()):
            source = Path(masks) / str(next(mask_ids))
            if not path.is_dir():
                source.touch(mode=0)
                return source
            source.mkdir()
            # Only structural mount points are present. Search-only ancestors
            # permit traversal to a named exception, never listing hidden peers.
            for entry in openings:
                target = checked_path(entry["path"])
                if target != path and target.is_relative_to(path):
                    destination = source / target.relative_to(path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if target.is_dir():
                        destination.mkdir(exist_ok=True)
                    else:
                        destination.touch(mode=0)
            if path == Path('/'):
                for name in ('proc', 'dev', 'run'):
                    (source / name).mkdir(exist_ok=True)
                if resolver_target:
                    destination = source / resolver_target.lstrip('/')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.touch(mode=0, exist_ok=True)
                # Preserve standard usr-merge aliases only when their targets
                # are explicitly visible; aliases do not grant extra content.
                for name in ('bin', 'sbin', 'lib', 'lib64'):
                    original = Path('/') / name
                    if original.is_symlink() and any(original.resolve().is_relative_to(Path(e['path'])) for e in openings):
                        (source / name).symlink_to(os.readlink(original))
            directories = [p for p in source.rglob('*') if p.is_dir() and not p.is_symlink()]
            for directory in reversed(directories):
                directory.chmod(0o111)
            source.chmod(0o111 if directories or any(source.iterdir()) else 0)
            return source

        # All policy modes use the same ancestor-to-descendant ordering.
        # Sources are pinned in the host namespace before bwrap masks parents.
        for entry in sorted(policy, key=lambda e: (len(Path(e["path"]).parts), e["path"])):
            path = checked_path(entry["path"])
            if path == Path('/') and entry['access'] != 'deny':
                continue
            if str(path) in config.get('private_tmp', []):
                if str(path) not in ('/tmp', '/var/tmp') or entry['access'] != 'write':
                    raise ValueError('Invalid private temporary mount')
                args += ['--tmpfs', str(path)]
                continue
            source = mask(path, config['mounts']) if entry['access'] == 'deny' else path
            args += ["--bind-fd" if entry['access'] == 'write' else "--ro-bind-fd", pinned(source), str(path)]
            if path == Path('/') and entry['access'] == 'deny':
                args += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/run', '--dir', '/run/aster-harness']
        # Intrinsic host/runtime secrets are a separate non-overridable layer.
        for value in config.get('protected_deny', []):
            path = checked_path(value)
            covering = [e for e in policy if path.is_relative_to(Path(e['path']))]
            already_denied = bool(covering) and max(covering, key=lambda e: len(Path(e['path']).parts))['access'] == 'deny'
            reopened = any(Path(e['path']).is_relative_to(path) for e in config['mounts'])
            if already_denied and not reopened:
                continue
            openings = [{"path": str(resolver)}] if resolver is not None and resolver.is_relative_to(path) else []
            args += ['--ro-bind-fd', pinned(mask(path, openings)), str(path)]
        # Restore only the host-selected resolver file. Its symlink remains
        # intact; hidden siblings stay absent and the file cannot be modified.
        if resolver is not None:
            args += ['--ro-bind-fd', pinned(resolver), resolver_target or str(resolver)]
        # Only trusted host-generated roles can expose state at synthetic paths.
        for grant in config.get('grants', []):
            path = checked_path(grant['source'])
            args += ['--bind-fd' if grant['access'] == 'write' else '--ro-bind-fd',
                     pinned(path), grant['target']]
        # Keep /dev minimal, then add explicitly authorized devices. Ordinary
        # binds use nodev; these need dev-bind. Pin sources and reject symlinks.
        for value in config.get('devices', []):
            if value != '/dev/dxg':
                raise ValueError('Unsupported GPU device')
            path = checked_path(value)
            fd = int(pinned(path))
            if not stat.S_ISCHR(os.fstat(fd).st_mode):
                raise ValueError('GPU source must be a character device')
            args += ['--dev-bind', '/proc/self/fd/' + str(fd), str(path)]
        # This procfs belongs to the execution's PID namespace. Keep process
        # bookkeeping (including child uid/gid maps) writable, while protecting
        # kernel controls. Never mount the host's process tree here.
        for value in ("/proc/sys", "/proc/fs", "/proc/irq", "/proc/bus"):
            path = Path(value)
            if path.exists():
                args += ["--ro-bind-fd", pinned(path), value]
        if Path("/proc/sysrq-trigger").exists():
            args += ["--ro-bind", "/dev/null", "/proc/sysrq-trigger"]
        seccomp = seccomp_fd(local_ipc=config.get("local_ipc", False))
        os.set_inheritable(seccomp, True)
        args += [
            "--remount-ro",
            "/dev",
            "--remount-ro",
            "/run",
            # POSIX shared memory belongs to this execution tree, never the
            # host. Keep /dev itself read-only; only this child mount is writable.
            "--tmpfs",
            "/dev/shm",
            "--seccomp",
            str(seccomp),
            "--chdir",
            config["cwd"],
            "--",
            *argv,
        ]
        # bwrap consumes mount descriptors and installs no_new_privs/seccomp before exec.
        child = subprocess.Popen(args, pass_fds=(*descriptors, seccomp))

        def forward(signum, frame):
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            return child.wait()
        finally:
            for fd in (*descriptors, seccomp):
                os.close(fd)


if __name__ == "__main__":
    try:
        if len(sys.argv) < 4 or sys.argv[2] != "--":
            raise ValueError("Expected configuration JSON -- command")
        raise SystemExit(launch(json.loads(sys.argv[1]), sys.argv[3:]))
    except Exception as error:
        print(
            "Sandbox launch failed: " + type(error).__name__ + ": " + str(error),
            file=sys.stderr,
        )
        raise SystemExit(125)
