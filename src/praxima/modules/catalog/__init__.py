"""Domain-neutral business data: entity types, entities, relations, availability.

Public interface for other modules and entrypoints.
"""

from typing import TYPE_CHECKING

from praxima.shared.lazy import lazy_exports

if TYPE_CHECKING:  # real imports for type checkers; loaded lazily at runtime
    from praxima.modules.catalog.application.selectors import (
        EntityTypeView,
        EntityView,
        ExceptionView,
        RelationTypeView,
        RelationView,
        RuleView,
        availability_of,
        entities_page,
        entity_types,
        get_entity,
        relation_types,
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

_EXPORTS = {
    "EntityTypeView": "praxima.modules.catalog.application.selectors",
    "EntityView": "praxima.modules.catalog.application.selectors",
    "ExceptionView": "praxima.modules.catalog.application.selectors",
    "RelationTypeView": "praxima.modules.catalog.application.selectors",
    "RelationView": "praxima.modules.catalog.application.selectors",
    "RuleView": "praxima.modules.catalog.application.selectors",
    "availability_of": "praxima.modules.catalog.application.selectors",
    "entities_page": "praxima.modules.catalog.application.selectors",
    "entity_types": "praxima.modules.catalog.application.selectors",
    "get_entity": "praxima.modules.catalog.application.selectors",
    "relation_types": "praxima.modules.catalog.application.selectors",
    "relations_of": "praxima.modules.catalog.application.selectors",
    "AvailabilityExceptionDraft": "praxima.modules.catalog.application.services",
    "AvailabilityRuleDraft": "praxima.modules.catalog.application.services",
    "EntityChanges": "praxima.modules.catalog.application.services",
    "EntityDraft": "praxima.modules.catalog.application.services",
    "add_availability_exception": "praxima.modules.catalog.application.services",
    "add_availability_rule": "praxima.modules.catalog.application.services",
    "create_entity": "praxima.modules.catalog.application.services",
    "create_relation": "praxima.modules.catalog.application.services",
    "delete_entity": "praxima.modules.catalog.application.services",
    "delete_relation": "praxima.modules.catalog.application.services",
    "install_pack": "praxima.modules.catalog.application.services",
    "set_detail_status": "praxima.modules.catalog.application.services",
    "set_entity_status": "praxima.modules.catalog.application.services",
    "update_entity": "praxima.modules.catalog.application.services",
}
__getattr__, __dir__ = lazy_exports(__name__, _EXPORTS)
__all__ = [
    "AvailabilityExceptionDraft",
    "AvailabilityRuleDraft",
    "EntityChanges",
    "EntityDraft",
    "EntityTypeView",
    "EntityView",
    "ExceptionView",
    "RelationTypeView",
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
    "relation_types",
    "relations_of",
    "set_detail_status",
    "set_entity_status",
    "update_entity",
]
