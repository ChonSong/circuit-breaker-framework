"""Circuit Breaker Framework core."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger()


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class AssumptionState(Enum):
    HOLDING = "holding"
    TRIPPED = "tripped"
    BYPASSED = "bypassed"


@dataclass
class Assumption:
    id: str
    name: str
    description: str | None = None
    max_rate: float | None = None
    min_value: float | None = None
    max_value: float | None = None


@dataclass
class AssumptionCheck:
    assumption_id: str
    timestamp: datetime.datetime
    value: float | None = None
    rate: float | None = None
    count: int | None = None
    passed: bool = True
    details: str | None = None


@dataclass
class CircuitBreakerEvent:
    id: str
    timestamp: datetime.datetime
    assumption_id: str | None
    from_state: CircuitState | None
    to_state: CircuitState
    reason: str
    checks: list[AssumptionCheck] = field(default_factory=list)


class AssumptionRegistry:
    def __init__(self):
        self._assumptions: dict[str, Assumption] = {}
        self._check_history: dict[str, list[AssumptionCheck]] = {}

    def declare(self, name: str, **kwargs) -> Assumption:
        a = Assumption(id=str(uuid.uuid4()), name=name, **kwargs)
        self._assumptions[name] = a
        self._check_history[a.id] = []
        logger.info("assumption_declared", assumption_id=a.id, name=name)
        return a

    def check(
        self,
        assumption_name: str,
        value: float | None = None,
        rate: float | None = None,
        count: int | None = None,
        details: str | None = None,
    ) -> AssumptionCheck:
        a = self._assumptions.get(assumption_name)
        if a is None:
            raise ValueError(f"Unknown assumption: {assumption_name}")

        passed = True
        reason = "ok"

        if a.max_rate is not None and rate is not None:
            if rate > a.max_rate:
                passed = False
                reason = f"rate {rate:.3f} exceeds max {a.max_rate}"

        if a.min_value is not None and value is not None:
            if value < a.min_value:
                passed = False
                reason = f"value {value:.3f} below min {a.min_value}"

        if a.max_value is not None and value is not None:
            if value > a.max_value:
                passed = False
                reason = f"value {value:.3f} above max {a.max_value}"

        check = AssumptionCheck(
            assumption_id=a.id,
            timestamp=datetime.datetime.utcnow(),
            value=value,
            rate=rate,
            count=count,
            passed=passed,
            details=reason if not passed else details,
        )
        self._check_history[a.id].append(check)
        logger.info("assumption_check", assumption=assumption_name, passed=passed, reason=reason)
        return check

    def recent_rate(self, assumption_name: str, window: int = 100) -> float:
        a = self._assumptions.get(assumption_name)
        if a is None:
            return 0.0
        checks = self._check_history[a.id][-window:]
        if not checks:
            return 0.0
        return sum(1 for c in checks if not c.passed) / len(checks)


class CircuitBreaker:
    def __init__(self):
        self.assumptions = AssumptionRegistry()
        self.state = CircuitState.CLOSED
        self.events: list[CircuitBreakerEvent] = []
        self._tripped_assumption: str | None = None

    def declare(self, name: str, **kwargs) -> Assumption:
        return self.assumptions.declare(name=name, **kwargs)

    def monitor(
        self,
        assumption_name: str,
        context: dict[str, Any] | None = None,
    ) -> AssumptionCheck:
        value = context.get("value") if context else None
        rate = context.get("rate") if context else None
        count = context.get("count") if context else None
        details = context.get("details") if context else None
        check = self.assumptions.check(
            assumption_name, value=value, rate=rate, count=count, details=details
        )
        if not check.passed and self.state == CircuitState.CLOSED:
            self._trip(assumption_name, check.details or "assumption failed")
        return check

    def _trip(self, assumption_name: str, reason: str) -> None:
        prev = self.state
        self.state = CircuitState.OPEN
        self._tripped_assumption = assumption_name
        event = CircuitBreakerEvent(
            id=str(uuid.uuid4()),
            timestamp=datetime.datetime.utcnow(),
            assumption_id=assumption_name,
            from_state=prev,
            to_state=CircuitState.OPEN,
            reason=reason,
        )
        self.events.append(event)
        logger.warning("circuit_breaker_tripped", assumption=assumption_name, reason=reason)

    def review_complete(self) -> None:
        if self.state != CircuitState.OPEN:
            return
        self.state = CircuitState.HALF_OPEN
        logger.info("circuit_breaker_half_open", assumption=self._tripped_assumption)

    def reset(self) -> None:
        prev = self.state
        self.state = CircuitState.CLOSED
        self._tripped_assumption = None
        self.events.append(CircuitBreakerEvent(
            id=str(uuid.uuid4()),
            timestamp=datetime.datetime.utcnow(),
            assumption_id=None,
            from_state=prev,
            to_state=CircuitState.CLOSED,
            reason="manual_reset",
        ))
        logger.info("circuit_breaker_reset")
