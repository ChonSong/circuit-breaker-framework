"""AIE Orchestrator Node -- wraps CircuitBreaker with drift scoring, provenance, and HitL."""
from __future__ import annotations
import datetime, uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import structlog
from circuit_breaker import CircuitBreaker, CircuitState
logger = structlog.get_logger()

class DriftScore(float, Enum):
    STABLE = 0.0; LOW = 0.25; MODERATE = 0.5; HIGH = 0.75; CRITICAL = 0.9; SEVERE = 1.0
    @classmethod
    def from_value(cls, value):
        if value < 0.25: return cls.STABLE
        elif value < 0.5: return cls.LOW
        elif value < 0.75: return cls.MODERATE
        elif value < 0.9: return cls.HIGH
        elif value < 1.0: return cls.CRITICAL
        return cls.SEVERE

class HaltReason(Enum):
    SEMANTIC_DRIFT = "semantic_drift"
    OPERATIONAL_EXHAUSTION = "operational_exhaustion"
    BLAST_RADIUS_VIOLATION = "blast_radius_violation"
    RAG_TOXICITY_LOW = "rag_toxicity_low"
    CIRCUIT_OPEN = "circuit_open"

@dataclass
class HitLDiagnostic:
    id: str
    timestamp: datetime.datetime
    halt_reason: HaltReason
    drift_score: float | None = None
    provenance_chain: list = field(default_factory=list)
    failed_assumptions: list = field(default_factory=list)
    resource_burn_remaining: float | None = None
    blast_radius_actual: float | None = None
    blast_radius_max: float | None = None
    rag_toxicity_pct: float | None = None
    delegation_payload: dict = field(default_factory=dict)
    recovery_suggestions: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    def to_dict(self):
        return {
            "id": self.id, "timestamp": self.timestamp.isoformat(),
            "halt_reason": self.halt_reason.value, "drift_score": self.drift_score,
            "provenance_chain": self.provenance_chain, "failed_assumptions": self.failed_assumptions,
            "resource_burn_remaining": self.resource_burn_remaining,
            "blast_radius_actual": self.blast_radius_actual, "blast_radius_max": self.blast_radius_max,
            "rag_toxicity_pct": self.rag_toxicity_pct,
            "delegation_payload": self.delegation_payload,
            "recovery_suggestions": self.recovery_suggestions, "metadata": self.metadata,
        }

@dataclass
class ProvenanceNode:
    id: str; label: str; parent_id: str | None = None; depth: int = 0
    metadata: dict = field(default_factory=dict)
    created_at: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    def to_dict(self):
        return {"id": self.id, "label": self.label, "parent_id": self.parent_id,
                "depth": self.depth, "metadata": self.metadata, "created_at": self.created_at.isoformat()}

class ProvenanceGraph:
    def __init__(self):
        self._nodes = {}; self._root_id = None
    def add_root(self, label, metadata=None):
        node = ProvenanceNode(id=str(uuid.uuid4()), label=label, depth=0, metadata=metadata or {})
        self._nodes[node.id] = node; self._root_id = node.id; return node
    def add_child(self, parent_id, label, metadata=None):
        parent = self._nodes.get(parent_id)
        depth = (parent.depth + 1) if parent else 0
        node = ProvenanceNode(id=str(uuid.uuid4()), label=label, parent_id=parent_id, depth=depth, metadata=metadata or {})
        self._nodes[node.id] = node; return node
    def chain(self):
        if self._root_id is None: return []
        current = self._root_id; path = [current]
        while True:
            children = [n for n in self._nodes.values() if n.parent_id == current]
            if not children: break
            current = children[-1].id; path.append(current)
        return path
    def labels(self):
        return [self._nodes[nid].label for nid in self.chain()]
    def to_dict(self):
        return {"root_id": self._root_id, "nodes": {nid: n.to_dict() for nid, n in self._nodes.items()}, "chain": self.chain(), "labels": self.labels()}

class OrchestratorNode:
    def __init__(self, name, drift_threshold=0.9, resource_burn_max=None, blast_radius_max=None, rag_toxicity_min=60.0):
        self.name = name; self.drift_threshold = drift_threshold
        self.resource_burn_max = resource_burn_max; self.blast_radius_max = blast_radius_max; self.rag_toxicity_min = rag_toxicity_min
        self._circuit = CircuitBreaker(); self._drift_score = 0.0
        self._resource_burn_remaining = resource_burn_max
        self._blast_radius_actual = 0.0; self._rag_toxicity_pct = 100.0
        self._halted = False; self._halt_reason = None
        self._prov_graph = ProvenanceGraph(); self._current_prov_id = None
        self._hitl_queue = []; self._delegate_payloads = []
        root = self._prov_graph.add_root(label=f"orchestrator:{self.name}", metadata={"drift_threshold": self.drift_threshold, "resource_burn_max": self.resource_burn_max, "blast_radius_max": self.blast_radius_max, "rag_toxicity_min": self.rag_toxicity_min})
        self._current_prov_id = root.id
        logger.info("orchestrator_node_initialized", name=self.name, drift_threshold=self.drift_threshold)
    def declare(self, name, **kwargs): return self._circuit.declare(name=name, **kwargs)
    def execute(self, task_label, context=None, delegate=False, delegate_model=None, delegate_instructions=None):
        if self._halted: return self._make_response(halted=True)
        prov_node = self._prov_graph.add_child(parent_id=self._current_prov_id or "", label=task_label, metadata={"context_keys": list(context.keys()) if context else []})
        self._current_prov_id = prov_node.id
        if delegate:
            payload = self._build_delegation_payload(task_label, context or {}, delegate_model, delegate_instructions)
            self._delegate_payloads.append(payload)
        self._check_halt_conditions(context)
        if self._halted:
            diagnostic = self._package_hitl()
            return self._make_response(halted=True, diagnostic=diagnostic)
        return self._make_response(halted=False)
    def update_drift(self, score):
        self._drift_score = float(max(0.0, min(1.0, score)))
        if self._drift_score >= self.drift_threshold:
            self._halt(HaltReason.SEMANTIC_DRIFT, f"drift {self._drift_score:.3f} >= {self.drift_threshold}")
    def update_resource_burn(self, units):
        if self._resource_burn_remaining is None: return
        self._resource_burn_remaining = max(0.0, self._resource_burn_remaining - units)
        if self._resource_burn_remaining <= 0: self._halt(HaltReason.OPERATIONAL_EXHAUSTION, "resource budget depleted")
    def update_blast_radius(self, actual):
        self._blast_radius_actual = float(actual)
        if self.blast_radius_max is not None and self._blast_radius_actual > self.blast_radius_max:
            self._halt(HaltReason.BLAST_RADIUS_VIOLATION, f"blast radius {self._blast_radius_actual:.3f} > max {self.blast_radius_max}")
    def update_rag_toxicity(self, pct):
        self._rag_toxicity_pct = float(pct)
        if self._rag_toxicity_pct < self.rag_toxicity_min:
            self._halt(HaltReason.RAG_TOXICITY_LOW, f"RAG toxicity {self._rag_toxicity_pct:.1f}% < min {self.rag_toxicity_min}%")
    def monitor(self, assumption_name, context=None):
        check = self._circuit.monitor(assumption_name, context)
        if self._circuit.state == CircuitState.OPEN and not self._halted:
            self._halt(HaltReason.CIRCUIT_OPEN, f"assumption {assumption_name} tripped")
        return check
    def review_complete(self):
        self._circuit.review_complete()
        if self._circuit.state != CircuitState.OPEN:
            self._halted = False; self._halt_reason = None; self._hitl_queue.clear()
    def reset(self):
        self._circuit.reset(); self._drift_score = 0.0
        if self._resource_burn_remaining is not None and self.resource_burn_max is not None: self._resource_burn_remaining = self.resource_burn_max
        self._blast_radius_actual = 0.0; self._rag_toxicity_pct = 100.0
        self._halted = False; self._halt_reason = None; self._hitl_queue.clear(); self._delegate_payloads.clear()
    @property
    def state(self): return self._circuit.state
    @property
    def provenance(self): return self._prov_graph
    @property
    def drift_label(self): return DriftScore.from_value(self._drift_score)
    def pop_hitl_queue(self): queue = list(self._hitl_queue); self._hitl_queue.clear(); return queue

    def _check_halt_conditions(self, context=None):
        ctx = context or {}
        drift_val = ctx.get("drift_score", self._drift_score)
        if drift_val >= self.drift_threshold:
            self._halt(HaltReason.SEMANTIC_DRIFT, f"drift {drift_val:.3f} >= {self.drift_threshold}"); return
        if self._resource_burn_remaining is not None and self._resource_burn_remaining <= 0:
            self._halt(HaltReason.OPERATIONAL_EXHAUSTION, "resource budget depleted"); return
        if self.blast_radius_max is not None:
            actual = ctx.get("blast_radius", self._blast_radius_actual)
            if actual > self.blast_radius_max:
                self._halt(HaltReason.BLAST_RADIUS_VIOLATION, f"blast radius {actual:.3f} > max {self.blast_radius_max}"); return
        if self._rag_toxicity_pct < self.rag_toxicity_min:
            self._halt(HaltReason.RAG_TOXICITY_LOW, f"RAG toxicity {self._rag_toxicity_pct:.1f}% < min {self.rag_toxicity_min}%")
    def _halt(self, reason, details):
        if self._halted: return
        self._halted = True; self._halt_reason = reason
    def _package_hitl(self):
        if not self._halted or self._halt_reason is None: return None
        diagnostic = HitLDiagnostic(
            id=str(uuid.uuid4()), timestamp=datetime.datetime.utcnow(), halt_reason=self._halt_reason,
            drift_score=self._drift_score, provenance_chain=self._prov_graph.labels(),
            failed_assumptions=self._failed_assumption_names(), resource_burn_remaining=self._resource_burn_remaining,
            blast_radius_actual=self._blast_radius_actual, blast_radius_max=self.blast_radius_max,
            rag_toxicity_pct=self._rag_toxicity_pct,
            delegation_payload=self._delegate_payloads[-1] if self._delegate_payloads else {},
            recovery_suggestions=_suggest_recovery(self._halt_reason),
            metadata={"orchestrator_name": self.name, "circuit_state": self._circuit.state.value, "drift_label": self.drift_label.value},
        )
        self._hitl_queue.append(diagnostic); return diagnostic
    def _build_delegation_payload(self, task_label, context, model, instructions):
        return {"task": task_label, "model": model, "instructions": instructions,
                "orchestrator_name": self.name, "provenance_chain": self._prov_graph.labels(),
                "drift_score": self._drift_score, "drift_label": self.drift_label.value,
                "circuit_state": self._circuit.state.value, "context": context,
                "resource_burn_remaining": self._resource_burn_remaining,
                "rag_toxicity_pct": self._rag_toxicity_pct, "timestamp": datetime.datetime.utcnow().isoformat()}
    def _failed_assumption_names(self):
        return [e.assumption_id or "unknown" for e in self._circuit.events if e.to_state == CircuitState.OPEN]
    def _make_response(self, halted, diagnostic=None):
        return {"provenance_id": self._current_prov_id, "halted": halted,
                "halt_reason": self._halt_reason.value if self._halt_reason else None,
                "drift_score": self._drift_score, "drift_label": self.drift_label.value,
                "circuit_state": self._circuit.state.value, "resource_burn_remaining": self._resource_burn_remaining,
                "rag_toxicity_pct": self._rag_toxicity_pct, "hitl_diagnostic": diagnostic.to_dict() if diagnostic else None}

def _suggest_recovery(reason):
    suggestions = {
        HaltReason.SEMANTIC_DRIFT: ["Review recent outputs for distributional shift", "Retrain or fine-tune on recent data", "Narrow the task scope to reduce drift surface"],
        HaltReason.OPERATIONAL_EXHAUSTION: ["Increase resource budget if appropriate", "Optimize prompting to reduce token consumption", "Offload non-critical subtasks to cheaper models"],
        HaltReason.BLAST_RADIUS_VIOLATION: ["Reduce batch size or scope of affected operations", "Add containment guards around high-impact actions", "Review and tighten blast radius threshold"],
        HaltReason.RAG_TOXICITY_LOW: ["Audit the retrieval corpus for quality and relevance", "Improve chunking strategy or embedding model", "Add a re-ranker to filter low-quality retrieved documents"],
        HaltReason.CIRCUIT_OPEN: ["Review tripped assumption with human expert", "Adjust assumption threshold if too aggressive", "Inspect recent assumption check history for patterns"],
    }
    return suggestions.get(reason, ["Review diagnostic package for details."])
