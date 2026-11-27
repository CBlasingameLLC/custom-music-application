"""What a Subsonic client sent: the query string, and, for a POST, the form as well (OpenSubsonic's `formPost`). Names repeat (`id`, `songId`...)."""

from __future__ import annotations

from collections.abc import Iterable

from musictoolkit.web.subsonic.errors import GENERIC, SubsonicError, missing


class Params:
    def __init__(self, *sources: Iterable[tuple[str, str]]) -> None:
        self._items: list[tuple[str, str]] = [(name, value) for source in sources for name, value in source]

    def get(self, name: str) -> str | None:
        """The first value sent under this name (an empty one counts as not sent)."""
        for key, value in self._items:
            if key == name and value != "":
                return value
        return None

    def getlist(self, name: str) -> list[str]:
        return [value for key, value in self._items if key == name and value != ""]

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise missing(name)
        return value

    def integer(self, name: str, default: int | None = None, *, low: int | None = None, high: int | None = None) -> int | None:
        """A whole number, kept within `low` and `high` (a client asking for 10,000 songs gets the most there is room for, not an error)."""
        value = self.get(name)
        if value is None:
            return default
        try:
            number = int(value)
        except ValueError:
            raise SubsonicError(GENERIC, f"{name} is not a whole number") from None
        if low is not None:
            number = max(low, number)
        if high is not None:
            number = min(high, number)
        return number

    def number(self, name: str, default: float = 0.0, *, low: float = 0.0) -> float:
        """A number of seconds, not below `low` (a client that sends something that is not a number is told so)."""
        value = self.get(name)
        if value is None:
            return default
        try:
            number = float(value)
        except ValueError:
            raise SubsonicError(GENERIC, f"{name} is not a number") from None
        if number != number or number in (float("inf"), float("-inf")):  # (nan and infinity are not seconds)
            raise SubsonicError(GENERIC, f"{name} is not a number")
        return max(low, number)

    def boolean(self, name: str, default: bool = False) -> bool:
        value = self.get(name)
        if value is None:
            return default
        return value.strip().lower() in ("true", "1", "yes")

    def names(self) -> set[str]:
        return {name for name, _ in self._items}
