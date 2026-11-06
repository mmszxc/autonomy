"""Deterministic validation primitives; proposal code must not own these gates."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class FrozenValidator:
    name: str
    expected_hash: str
    author: str
    proposer: str
    source: bytes

    def __post_init__(self):
        if self.author == self.proposer:
            raise ValidationError("validator author must be independent from proposer")
        if len(self.expected_hash) != 64:
            raise ValidationError("validator hash must be sha256")

    def verify_source(self) -> bool:
        return hashlib.sha256(self.source).hexdigest() == self.expected_hash

    def accept_mutations(self, results: Iterable[bool], minimum: int = 20) -> bool:
        values = list(results)
        return self.verify_source() and len(values) >= minimum and all(values)


def non_inferior(*, candidate: float, control: float, margin: float, posterior: float,
                 minimum_posterior: float = 0.95) -> bool:
    return posterior >= minimum_posterior and candidate >= control - margin


@dataclass(frozen=True)
class StopRule:
    safety_violations: int = 0
    cost_increase: float = 0.10
    min_posterior: float = 0.95

    def should_stop(self, *, safety_violations: int, cost_increase: float, posterior: float) -> bool:
        if safety_violations > self.safety_violations:
            return True
        if cost_increase > self.cost_increase:
            return True
        return posterior < self.min_posterior


def verify_validator_hash(source: bytes, expected_hash: str) -> None:
    if hashlib.sha256(source).hexdigest() != expected_hash:
        raise ValidationError("validator hash mismatch")
