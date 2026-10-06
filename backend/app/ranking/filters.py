import json
from dataclasses import dataclass

from sqlalchemy import (
    ColumnElement,
    ColumnExpressionArgument,
    exists,
    false,
    func,
    literal,
    select,
)

from app.models import RankingItem, Repository

MAX_SQL_INT = 2**63 - 1


@dataclass(frozen=True, slots=True)
class RankingFilters:
    """Canonical ranking filter parameters shared by SQL predicates and cache keys."""

    language: str | None = None
    topic: str | None = None
    min_stars: int = 0
    query: str | None = None

    def parameters(self) -> list[str | int | None]:
        """Structured parameter list preserving ``None`` versus literal values.

        Only string filters are lowercased so the cache key merges requests that
        the SQL predicates already treat as equal. ``None``, empty strings,
        ``"-"`` and whitespace are preserved verbatim: no strip, no casefold.
        """
        return [
            _lower(self.language),
            _lower(self.topic),
            self.min_stars,
            _lower(self.query),
        ]

    def predicates(self, dialect_name: str) -> list[ColumnElement[bool]]:
        predicates: list[ColumnElement[bool]] = []
        language_predicate = self._language_predicate(dialect_name)
        if language_predicate is not None:
            predicates.append(language_predicate)
        topic_predicate = self._topic_predicate(dialect_name)
        if topic_predicate is not None:
            predicates.append(topic_predicate)
        predicates.append(_min_stars_predicate(self.min_stars))
        query_predicate = self._query_predicate(dialect_name)
        if query_predicate is not None:
            predicates.append(query_predicate)
        return predicates

    def _language_predicate(self, dialect_name: str) -> ColumnElement[bool] | None:
        if not self.language:
            return None
        # PostgreSQL text columns reject NUL bytes; a NUL language can never
        # match a stored value, so answer with a constant false predicate.
        # SQLite can store NUL, so it is left to the database comparison.
        if dialect_name == "postgresql" and "\x00" in self.language:
            return false()
        return Repository.language_lower.collate(_collation(dialect_name)) == self.language.lower()

    def _topic_predicate(self, dialect_name: str) -> ColumnElement[bool] | None:
        if not self.topic:
            return None
        encoded = json.dumps(self.topic.lower(), ensure_ascii=True)
        elements = _topic_elements(dialect_name, Repository.topics_lower_keys)
        value = elements.c.value.collate(_collation(dialect_name))
        # Correlated EXISTS keeps one row per repository even when the stored
        # topic array contains duplicate elements.
        return exists(select(literal(1)).select_from(elements).where(value == encoded))

    def _query_predicate(self, dialect_name: str) -> ColumnElement[bool] | None:
        if not self.query:
            return None
        if dialect_name == "postgresql" and "\x00" in self.query:
            return false()
        lowered = self.query.lower()
        if dialect_name == "postgresql":
            return func.strpos(Repository.search_text_lower, lowered) > 0
        return func.instr(Repository.search_text_lower, lowered) > 0


def _lower(value: str | None) -> str | None:
    return value.lower() if value is not None else None


def _collation(dialect_name: str) -> str:
    return "C" if dialect_name == "postgresql" else "BINARY"


def _min_stars_predicate(min_stars: int) -> ColumnElement[bool]:
    if min_stars > MAX_SQL_INT:
        return false()
    return RankingItem.end_stars >= min_stars


def _topic_elements(dialect_name: str, column: ColumnExpressionArgument[list[str]]):
    if dialect_name == "postgresql":
        function = func.json_array_elements_text(column)
    else:
        function = func.json_each(column)
    return function.table_valued("value")
