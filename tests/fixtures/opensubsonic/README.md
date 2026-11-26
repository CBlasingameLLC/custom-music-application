# OpenSubsonic schemas (test data)

A verbatim copy of the JSON schemas the OpenSubsonic project publishes for its API (`endpoints/`, `responses/` and `schemas/` of
<https://github.com/opensubsonic/open-subsonic-api>), under the Apache License 2.0 (see `LICENSE` here). Retrieved on 2026-11-26 from the `main`
branch, which describes API version 1.16.1.

Only the tests use them (`tests/subsonic_schema.py`, `tests/test_subsonic_conformance.py`): they check that what the server answers is what the protocol
says. Nothing here is part of the program. To update them, copy the three folders over again.
