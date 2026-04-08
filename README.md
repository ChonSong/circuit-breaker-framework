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

## AIE Orchestrator Integration

The framework ships with an `OrchestratorNode` that wraps `CircuitBreaker` with AIE-grade observability and four semantic halt conditions.

### Architecture

```
Workflow
  └─ OrchestratorNode
       ├─ CircuitBreaker       (assumption-based CB, CLOSED/OPEN/HALF_OPEN)
       ├─ ProvenanceGraph      (DAG of all task executions)
       ├─ DriftScore           (STABLE → SEVERE, 0.0–1.0)
       └─ HitLDiagnostic       (human-readable halt package)
```

### Four Halt Conditions

| Condition | Threshold | Effect |
|---|---|---|
| Semantic drift | drift_score >= 0.9 | HALT + HitL diagnostic |
| Operational exhaustion | resource_burn_remaining <= 0 | HALT + HitL diagnostic |
| Blast radius violation | blast_radius_actual > blast_radius_max | HALT + HitL diagnostic |
| RAG toxicity | rag_toxicity_pct < 60% | HALT + HitL diagnostic |

### Usage

```python
from circuit_breaker.aie import OrchestratorNode

node = OrchestratorNode(
    name="doc-classifier",
    drift_threshold=0.9,
    resource_burn_max=100_000,   # tokens
    blast_radius_max=100,         # records
    rag_toxicity_min=60.0,
)

# Declare CB assumptions (standard)
node.declare("low_confidence_rate", max_rate=0.15)

# Execute tasks with full provenance
result = node.execute("classify_batch", context={"drift_score": 0.3, "blast_radius": 12})
# → halted: False

# Update runtime signals
node.update_drift(0.85)           # still OK
node.update_drift(0.95)           # → halted: True, reason: semantic_drift
node.update_resource_burn(5000)   # track token burn

# Delegate to sub-agent with MVC context payload
result = node.execute(
    "summarise",
    context={"text": "..."},
    delegate=True,
    delegate_model="openai/gpt-4o",
    delegate_instructions="Summarise briefly",
)
# delegation_payload is queued in the response

# Inspect HitL diagnostics
diagnostics = node.pop_hitl_queue()
for d in diagnostics:
    print(d.halt_reason.value, d.recovery_suggestions)

# Human reviews and resumes
node.review_complete()   # acknowledges review, resumes if circuit is half-open
node.reset()             # full reset
```

### HitL Diagnostic Package

When the orchestrator halts, it packages a `HitLDiagnostic` containing:

- `halt_reason` — which of the 4 conditions triggered the halt
- `drift_score` / `drift_label` — STABLE through SEVERE
- `provenance_chain` — ordered list of all task labels executed
- `failed_assumptions` — CB assumption names that tripped
- `resource_burn_remaining` — tokens/calls left at halt
- `blast_radius_actual / _max` — scope of affected units
- `rag_toxicity_pct` — retrieval quality at halt
- `delegation_payload` — last sub-agent delegation context
- `recovery_suggestions` — action recommendations per halt reason

### Provenance Graph

Every `execute()` call appends a node to a DAG. The chain is readable at any time:

```python
node.provenance.labels()    # ["orchestrator:doc-classifier", "classify_batch", "summarise"]
node.provenance.to_dict()   # full serialisable graph
```

### Oracles

The `oracles/` directory contains reference YAML configurations for each halt condition:

| Oracle | File |
|---|---|
| Semantic drift monitoring | `oracles/semantic_drift_oracle.yaml` |
| Resource exhaustion tracking | `oracles/resource_exhaustion_oracle.yaml` |
| Blast radius guard | `oracles/blast_radius_oracle.yaml` |
| RAG quality validation | `oracles/rag_toxicity_oracle.yaml` |

Oracles define the threshold, sampling interval, and recovery action suggestions for each condition. They serve as deployment manifests for the corresponding `OrchestratorNode` configuration.

## Tech Stack

- **Python 3.11+** — Pydantic, Structlog, PyYAML, APScheduler
