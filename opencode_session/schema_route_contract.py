from dataclasses import dataclass
from typing import Tuple

from opencode_session.schema_helpers import (
    child_value,
    first_not_none,
    first_present,
    root_or_info_value,
)


@dataclass(frozen=True)
class RouteField:
    name: str
    aliases: Tuple[str, ...] = ()
    children: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    include_info: bool = True

    def read(self, record):
        value = None
        if self.aliases:
            if self.include_info:
                value = root_or_info_value(record, *self.aliases)
            else:
                value = first_present(record, *self.aliases)
        if value is not None:
            return value
        return first_not_none(
            *(child_value(record, child_name, *aliases) for child_name, aliases in self.children)
        )


@dataclass(frozen=True)
class RouteAdapterContract:
    route: str
    version: str
    fields: Tuple[RouteField, ...] = ()
    known_fields: Tuple[str, ...] = ()
    minimum_field_sets: Tuple[Tuple[str, ...], ...] = ()

    def read_fields(self, record):
        return {field.name: field.read(record) for field in self.fields}

    def has_known_shape(self, fields):
        return any(fields[name] is not None for name in self.known_fields)

    def has_minimum_shape(self, fields):
        if not self.minimum_field_sets:
            return self.has_known_shape(fields)
        return any(
            all(fields.get(name) is not None for name in field_set)
            for field_set in self.minimum_field_sets
        )


def route_field(name, *aliases, children=(), include_info=True):
    return RouteField(name, tuple(aliases), tuple(children), include_info)


def child_field(name, *aliases):
    return name, tuple(aliases)
