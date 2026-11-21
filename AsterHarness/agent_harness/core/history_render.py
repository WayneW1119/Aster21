"""Compatibility shim: native history no longer receives display annotations."""


def numbered_input(items, labels=None):
    """Legacy callers keep native items; use query_history_directory for numbers."""
    return items
