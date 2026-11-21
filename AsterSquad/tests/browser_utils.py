"""Prefer Playwright's installed browser, with an optional local executable override."""
import os
from pathlib import Path


def chromium_executable(playwright):
    override=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE')
    if override:
        return override
    default=playwright.chromium.executable_path
    if Path(default).is_file():
        return default
    cached=sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux*/chrome'))
    if cached:
        return str(cached[-1])
    raise RuntimeError('Run python -m playwright install chromium first')
