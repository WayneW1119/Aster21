"""Deployment data is independent of the source checkout."""
import os
from pathlib import Path


def default_root():
    return str(Path(os.environ.get('ASTER_SQUAD_HOME', '~/.aster-squad')).expanduser())
