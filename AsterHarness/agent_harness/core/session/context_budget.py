"""Advisory context budget, based on provider-reported input usage, not billing totals.

Like Codex's context-window accounting, this lives in the runtime. Unlike its
auto-compaction path, crossing a threshold only produces an ordinary input event.
"""


def validate_warning(state: dict) -> dict:
    fields = {"generation", "threshold", "level", "input_tokens"}
    if (not isinstance(state, dict) or set(state) != fields
            or any(type(state[key]) is not int for key in fields)
            or state["generation"] < 0 or state["threshold"] < 1
            or state["level"] < 0 or state["input_tokens"] < state["threshold"]
            or state["level"] != (state["input_tokens"] - state["threshold"])
                // max(1, state["threshold"] // 4)):
        raise ValueError("Invalid context budget warning state")
    return dict(state)


class ContextBudget:
    def __init__(self, previous: dict | None = None) -> None:
        self.previous = validate_warning(previous) if previous is not None else None

    def warning(self, usage: dict | None, threshold: int | None,
                generation: int) -> dict | None:
        if threshold is None or not isinstance(usage, dict):
            return None
        tokens = usage.get("input_tokens")
        if type(tokens) is not int or tokens < threshold:
            return None
        state = {"generation": generation, "threshold": threshold,
                 "level": (tokens - threshold) // max(1, threshold // 4),
                 "input_tokens": tokens}
        previous = self.previous
        if (previous is not None and previous["generation"] == generation
                and previous["threshold"] == threshold
                and previous["level"] >= state["level"]):
            return None
        return state

    def commit(self, state: dict) -> None:
        self.previous = validate_warning(state)
