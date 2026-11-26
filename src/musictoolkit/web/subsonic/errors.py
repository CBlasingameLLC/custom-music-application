"""The errors a Subsonic client knows, by number (the table at subsonic.org/pages/api.jsp, and OpenSubsonic's additions for API keys)."""

from __future__ import annotations

from fastapi import HTTPException

GENERIC = 0
MISSING = 10  # a required parameter is missing
CLIENT_MUST_UPGRADE = 20
SERVER_MUST_UPGRADE = 30
WRONG_CREDENTIALS = 40
TOKEN_NOT_SUPPORTED = 41
MECHANISM_NOT_SUPPORTED = 42
CONFLICTING_MECHANISMS = 43
INVALID_API_KEY = 44
NOT_AUTHORIZED = 50
TRIAL_OVER = 60
NOT_FOUND = 70


class SubsonicError(Exception):
    """Something to answer with `status="failed"`: the number a client looks at, and words for a person."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def missing(name: str) -> SubsonicError:
    return SubsonicError(MISSING, f"Required parameter is missing: {name}")


def not_found(what: str = "data") -> SubsonicError:
    return SubsonicError(NOT_FOUND, f"The requested {what} was not found")


def from_http(error: HTTPException) -> SubsonicError:
    """What the rest of the app says with an HTTP status, as a client of this protocol hears it: the thing is not there (404), or has nothing in it to give
    (422: a file that is only tags), or the server cannot give it just now (503: the drive is not connected), which is the generic failure."""
    return SubsonicError(NOT_FOUND if error.status_code in (404, 422) else GENERIC, str(error.detail))
