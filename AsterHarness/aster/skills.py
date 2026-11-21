"""Public host-side access to the bundled skill catalog and source validation."""
from importlib.resources import files
from pathlib import Path
from agent_harness.core.skills.service import validate_sources


def builtin_directory():
    """Return the bundled skill directory in a normal filesystem installation."""
    return Path(str(files('agent_harness').joinpath('assets', 'skills')))


__all__ = ['builtin_directory', 'validate_sources']
