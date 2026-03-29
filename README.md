# Circuit Breaker Framework

Systems built on assumptions that eventually fail — without anyone noticing until the failure is expensive — are more common than systems that know when to stop.

Built a general-purpose circuit breaker framework that allows automated workflows to declare their assumptions explicitly, monitors whether those hold in practice, and halts gracefully — with a full audit trail — when they don't. Configured it around a document classification pipeline: when low-confidence classifications exceeded a threshold, the system paused and surfaced a human-review task.

## Features

- **Explicit assumption declarations** — workflows declare what they expect to be true
- **Runtime monitoring** — continuously check declared assumptions against real data
- **Graceful halting** — pause workflows with clear diagnostic information
- **Full audit trail** — every assumption check logged with context
- **Human-in-the-loop** — surface review tasks when thresholds exceeded

## Quick Start

```bash
pip install -e .
python -m circuit_breaker run --workflow examples/doc_classifier.yaml
```

## Example

```python
from circuit_breaker import CircuitBreaker

cb = CircuitBreaker()
cb.declare("low_confidence_rate", max_rate=0.15)
cb.monitor("low_confidence_rate", {"rate": 0.08})  # ok
cb.monitor("low_confidence_rate", {"rate": 0.22})  # trips circuit
```

## Tech Stack

- **Python 3.11+** — Pydantic, Structlog, PyYAML, APScheduler
