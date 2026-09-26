"""Pure work item rules: stage transitions declared by the pack's work item kind."""

from collections.abc import Sequence

from praxima.shared.errors import Conflict, FieldError, ValidationFailed


def check_transition(
    stages: Sequence[str], terminal: Sequence[str], current: str, target: str
) -> None:
    """Any declared stage is reachable, but never out of a terminal (closed) stage."""
    if target not in stages:
        raise ValidationFailed(errors=[FieldError("stage", "Not a stage of this kind.")])
    if target == current:
        raise Conflict("The item is already in this stage.")
    if current in terminal:
        raise Conflict("This item is closed; create a new one instead.")
