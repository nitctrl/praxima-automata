"""Domain-neutral business data: entity types, entities, relations, availability.

Public interface for other modules and entrypoints.
"""

from praxima.modules.catalog.application.selectors import (
    EntityTypeView,
    EntityView,
    ExceptionView,
    RelationView,
    RuleView,
    availability_of,
    entities_page,
    entity_types,
    get_entity,
    relations_of,
)
from praxima.modules.catalog.application.services import (
    AvailabilityExceptionDraft,
    AvailabilityRuleDraft,
    EntityChanges,
    EntityDraft,
    add_availability_exception,
    add_availability_rule,
    create_entity,
    create_relation,
    delete_entity,
    delete_relation,
    install_pack,
    set_detail_status,
    set_entity_status,
    update_entity,
)

__all__ = [
    "AvailabilityExceptionDraft",
    "AvailabilityRuleDraft",
    "EntityChanges",
    "EntityDraft",
    "EntityTypeView",
    "EntityView",
    "ExceptionView",
    "RelationView",
    "RuleView",
    "add_availability_exception",
    "add_availability_rule",
    "availability_of",
    "create_entity",
    "create_relation",
    "delete_entity",
    "delete_relation",
    "entities_page",
    "entity_types",
    "get_entity",
    "install_pack",
    "relations_of",
    "set_detail_status",
    "set_entity_status",
    "update_entity",
]
