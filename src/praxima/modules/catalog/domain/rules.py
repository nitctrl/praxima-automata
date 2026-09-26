"""Pure catalog rules: attribute validation against a pack JSON Schema, with safe messages."""

from typing import Any

from praxima.shared.errors import FieldError
from praxima.shared.validation import schema_errors


def attribute_errors(schema: dict[str, Any], attributes: dict[str, Any]) -> list[FieldError]:
    """All problems with an entity's attributes, one per field; empty when valid."""
    return schema_errors(schema, attributes, "attributes")
