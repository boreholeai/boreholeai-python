"""BoreholeAI Python SDK — digitise borehole logs programmatically."""

import logging as _logging

# Library best practice: attach a NullHandler so SDK log records don't
# fall through to Python's lastResort handler (which prints WARNING+ to
# stderr by default). Without this, internal `logger.warning` calls during
# a batch (e.g. transient poll errors) interleave with the per-file
# progress renderer's ANSI output and corrupt its cursor positioning.
# Users who DO want SDK logs can opt in with `logging.basicConfig()` etc.
_logging.getLogger("boreholeai").addHandler(_logging.NullHandler())

from boreholeai._types import FileResult, JobResult
from boreholeai._version import __version__
from boreholeai.client import BoreholeAI
from boreholeai.exceptions import (
    AuthenticationError,
    BoreholeAIError,
    InsufficientCreditsError,
    JobFailedError,
    RateLimitError,
    ServerError,
)

__all__ = [
    "BoreholeAI",
    "__version__",
    "AuthenticationError",
    "BoreholeAIError",
    "FileResult",
    "InsufficientCreditsError",
    "JobFailedError",
    "JobResult",
    "RateLimitError",
    "ServerError",
]
