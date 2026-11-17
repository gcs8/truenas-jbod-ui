"""Order-independent admission of observations that claim lookup keys.

Inventory builds many small tables that place a disk, a bay or a topology
member by a key such as a bay number, SAS address, serial or device path.
When two different observations claim one key, a plain dictionary keeps
whichever came first or last, so the answer depends on input order. This
index answers instead with one of three states:

- ``missing``: nothing claims the keys;
- ``unique``: exactly one claimant does, and its value is returned;
- ``ambiguous``: several claimants do (or one claimant named two different
  values). The value is withheld. The caller decides what that means; the
  index never turns it into ``missing``.

Rules, all independent of the order of ``add`` calls:

- A claimant identifies one observation. Several keys (aliases) of one
  claimant are not a collision. Two claimants on one key are, and stay so
  whatever arrives later at the same rank.
- ``rank`` competes within one key: a higher rank replaces every lower-ranked
  claim on that key (exact evidence over shifted evidence). Equal ranks tie.
- Scopes never collide. A key that needs several qualifiers can also carry
  them itself, for example ``(node, path)``.
- ``resolve`` takes the claims of every key it is given (each at that key's
  best rank) and unions them. It never takes the first hit. Pass one evidence
  tier per call; combine tiers in the caller.

Memory per key is bounded: at most ``CLAIMANT_LIMIT + 1`` distinct claimants
and their values are kept. Past that the key only remembers that it is
saturated, and ``claimant_count`` reports ``CLAIMANT_LIMIT + 1`` meaning "more
than the limit". Keys are never dropped: a thousand disks behind one expander
are a thousand unique keys, each admitted.

Pure standard-library code: no I/O, no logging, no import from the services
that will use it.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

T = TypeVar("T")

MISSING: Literal["missing"] = "missing"
UNIQUE: Literal["unique"] = "unique"
AMBIGUOUS: Literal["ambiguous"] = "ambiguous"
CLAIMANT_LIMIT = 8

State = Literal["missing", "unique", "ambiguous"]


class _Conflict:
    """Marks a claimant that named two unequal values for the same keys."""

    __slots__ = ()


_CONFLICT = _Conflict()


@dataclass(frozen=True)
class Resolution(Generic[T]):
    """Answer for one ``resolve`` call.

    ``claimants`` is the complete set of claimant ids when ``claimant_count``
    is within ``CLAIMANT_LIMIT``; past the limit it is ``None``, because which
    ids were kept would depend on input order.
    """

    state: State
    value: T | None
    claimant_count: int
    claimants: frozenset[Hashable] | None


class _Entry:
    __slots__ = ("rank", "claims")

    def __init__(self, rank: int) -> None:
        self.rank = rank
        # claimant -> value; None once more than CLAIMANT_LIMIT claimants are seen.
        self.claims: dict[Hashable, Any] | None = {}


def _merge(claims: dict[Hashable, Any], claimant: Hashable, value: Any) -> None:
    if claimant in claims:
        held = claims[claimant]
        if held is not _CONFLICT and not (held is value or held == value):
            claims[claimant] = _CONFLICT
        return
    claims[claimant] = value


class PlacementIndex(Generic[T]):
    """Collects ``(scope, key, claimant, value, rank)`` claims, then resolves keys."""

    def __init__(self) -> None:
        self._scopes: dict[Hashable, dict[Hashable, _Entry]] = {}

    def add(self, scope: Hashable, key: Hashable, claimant: Hashable, value: T, rank: int = 0) -> None:
        """Record that ``claimant`` claims ``key`` in ``scope`` with ``value``."""
        if not isinstance(rank, int) or isinstance(rank, bool):
            raise TypeError("rank must be an int")
        entries = self._scopes.setdefault(scope, {})
        entry = entries.get(key)
        if entry is None or rank > entry.rank:
            entry = entries[key] = _Entry(rank)
        elif rank < entry.rank:
            return
        claims = entry.claims
        if claims is None:
            return
        _merge(claims, claimant, value)
        if len(claims) > CLAIMANT_LIMIT:
            entry.claims = None

    def resolve(self, scope: Hashable, keys: Iterable[Hashable]) -> Resolution[T]:
        """Union the best-ranked claims of every key in ``keys`` within ``scope``."""
        if isinstance(keys, (str, bytes)):
            raise TypeError("keys must be an iterable of keys, not one string")
        entries = self._scopes.get(scope, {})
        claims: dict[Hashable, Any] = {}
        saturated = False
        for key in keys:
            entry = entries.get(key)
            if entry is None:
                continue
            if entry.claims is None:
                saturated = True
                continue
            for claimant, value in entry.claims.items():
                _merge(claims, claimant, value)
        if saturated or len(claims) > CLAIMANT_LIMIT:
            return Resolution(AMBIGUOUS, None, CLAIMANT_LIMIT + 1, None)
        claimants = frozenset(claims)
        if not claims:
            return Resolution(MISSING, None, 0, claimants)
        if len(claims) == 1:
            (value,) = claims.values()
            if value is not _CONFLICT:
                return Resolution(UNIQUE, value, 1, claimants)
        return Resolution(AMBIGUOUS, None, len(claims), claimants)

    def keys(self, scope: Hashable) -> frozenset[Hashable]:
        """Every key claimed in ``scope``, whatever its state."""
        return frozenset(self._scopes.get(scope, ()))

    def unique(self, scope: Hashable) -> dict[Hashable, T]:
        """``key -> value`` for each key in ``scope`` exactly one claimant holds.

        The contents never depend on input order; iteration order of the
        returned dictionary does, so sort it where order matters.
        """
        admitted: dict[Hashable, T] = {}
        for key in self._scopes.get(scope, {}):
            resolution = self.resolve(scope, (key,))
            if resolution.state == UNIQUE:
                admitted[key] = resolution.value  # type: ignore[assignment]
        return admitted
