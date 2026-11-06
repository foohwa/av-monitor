"""The one shape every site reader returns."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    kind: str
    external_id: str
    url: str
    title: str | None = None
    body: str | None = None
    published_at: str | None = None
    outbound_url: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Parsed:
    http_status: int
    final_url: str
    items: list[Item] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)
    native_id: str | None = None
    note: str | None = None
