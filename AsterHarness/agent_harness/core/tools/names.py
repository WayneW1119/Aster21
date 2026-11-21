"""Codex-compatible default identity for top-level function/custom tools."""

DEFAULT_FUNCTION_NAMESPACE = 'functions'


def is_default_namespace(namespace):
    return namespace is None or namespace == '' or namespace == DEFAULT_FUNCTION_NAMESPACE


def normalize_namespace(namespace):
    if namespace is not None and not isinstance(namespace, str):
        raise ValueError('namespace must be a string or omitted')
    return DEFAULT_FUNCTION_NAMESPACE if is_default_namespace(namespace) else namespace
