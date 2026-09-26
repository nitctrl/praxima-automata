"""JSON Schema validation with value-free messages (submitted values may be personal data)."""

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaViolation

from praxima.shared.errors import FieldError

# jsonschema messages echo submitted values; staff-facing messages must not (may be PII).
_MESSAGES = {
    "type": "has the wrong type",
    "required": "is required",
    "additionalProperties": "is not a known field",
    "minimum": "is too small",
    "exclusiveMinimum": "is too small",
    "maximum": "is too large",
    "exclusiveMaximum": "is too large",
    "minLength": "is too short",
    "maxLength": "is too long",
    "minItems": "has too few items",
    "maxItems": "has too many items",
    "pattern": "has an invalid format",
    "format": "has an invalid format",
    "enum": "is not an allowed value",
    "const": "is not an allowed value",
}


def _field(error: SchemaViolation, prefix: str) -> str:
    path = [str(part) for part in error.absolute_path]
    instance = error.instance if isinstance(error.instance, dict) else {}
    if error.validator == "required" and isinstance(error.validator_value, list):
        # The missing property is named in the schema, not in the (absent) instance path.
        missing = [str(name) for name in error.validator_value if name not in instance]
        path.append(missing[0] if missing else "?")
    if error.validator == "additionalProperties" and isinstance(error.schema, dict):
        known = error.schema.get("properties", {})
        extra = sorted(str(name) for name in instance if name not in known)
        path.append(extra[0] if extra else "?")
    return ".".join([prefix, *path])


def schema_errors(schema: dict[str, Any], data: dict[str, Any], prefix: str) -> list[FieldError]:
    """All problems, one per field (named `prefix.path`), ordered by field; empty when valid."""
    errors = {
        _field(e, prefix): FieldError(
            _field(e, prefix), _MESSAGES.get(str(e.validator), "is invalid")
        )
        for e in Draft202012Validator(schema).iter_errors(data)
    }
    return [errors[key] for key in sorted(errors)]
