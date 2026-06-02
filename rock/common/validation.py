"""Reusable Pydantic types for API request validation."""

from typing import Annotated

from pydantic import StringConstraints

NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
"""A string that cannot be empty or whitespace-only.

Used on request models / Path / Query / Body / Form parameters to reject
``""`` and ``"   "`` at deserialization time. ``strip_whitespace=True`` causes
Pydantic to trim surrounding whitespace before applying ``min_length=1``,
catching whitespace-only inputs.

Validation failures surface as ``RequestValidationError``, which the global
handler in ``rock.common.exception.request_validation_exception_handler`` maps
back to the project's ``RockResponse(status=Failed, error=...)`` envelope.
"""
