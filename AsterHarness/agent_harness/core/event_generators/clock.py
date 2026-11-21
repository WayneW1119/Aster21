"""Agent-owned fixed offset for interpreting and displaying wall-clock time."""
from datetime import datetime, timedelta, timezone
import re


class AgentClock:
    def __init__(self, path=None):
        self.path = path
        self.zone = timezone.utc
        if path is not None and path.exists():
            self.zone = self._parse(path.read_text().strip())

    @staticmethod
    def _parse(value):
        match = isinstance(value, str) and re.fullmatch(r"([+-])([0-9]{1,2})(?::([0-9]{2}))?", value)
        if not match:
            raise ValueError("Expected UTC offset such as +8, -05:00 or +05:30")
        sign, hours, minutes = match.groups()
        hours, minutes = int(hours), int(minutes or 0)
        if hours > 23 or minutes > 59:
            raise ValueError("Invalid UTC offset")
        return timezone(timedelta(minutes=(hours * 60 + minutes) * (1 if sign == "+" else -1)))

    def set(self, value):
        zone = self._parse(value)
        if self.path is not None:
            from ...agent_config import atomic_write
            atomic_write(self.path, datetime.now(zone).isoformat()[-6:] + "\n")
        self.zone = zone

    def deadline(self, value):
        if not isinstance(value, str) or not re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?", value):
            raise ValueError("Expected ISO date and time, optionally with UTC offset")
        if value[-6:-5] in ("+", "-"):
            self._parse(value[-6:])
        dt = datetime.fromisoformat(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=self.zone)).timestamp()
