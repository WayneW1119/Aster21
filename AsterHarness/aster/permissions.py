"""Path authorization decisions and host-selected runtime roots.

access_for selects the most specific ancestor (ties: deny > write > read).
covers checks whether requested rules stay within explicit granted authority;
unmatched paths are denied both in validation and in sandbox execution.
access_for/covers are pure; runtime_read_paths/gpu_devices inspect host paths.
"""
from agent_harness.core.sandbox.policy import access_for, covers
from agent_harness.core.sandbox import runtime_read_paths, gpu_devices

__all__ = ['access_for', 'covers', 'runtime_read_paths', 'gpu_devices']


def probe_windows_sandbox(executor):
    """Inspect the native Windows sandbox backend; no model requests."""
    from agent_harness.experimental.windows_mxc_probe import diagnose
    return diagnose(executor)


def smoke_windows_sandbox(executor):
    """Verify the backend with disposable files and a local loopback service."""
    from agent_harness.experimental.windows_mxc_smoke import smoke
    return smoke(executor)


__all__ += ['probe_windows_sandbox', 'smoke_windows_sandbox']
