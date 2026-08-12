"""Signal sources. Each exposes harvest(client, since_utc) -> (items, dropped)."""

from . import github, hn, lobsters

# Ordered cheapest/highest-signal first so a partial run still gets the good stuff.
ALL = {
    "hn": hn.harvest,
    "github": github.harvest,
    "lobsters": lobsters.harvest,
}

__all__ = ["ALL", "hn", "github", "lobsters"]
