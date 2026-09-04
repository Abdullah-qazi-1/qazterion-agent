"""Benchmarking, performance metrics, and reliability telemetry for Qazterion."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qz_tasks.database import connect, default_db_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ModelBenchmarkStats:
    model: str
    provider: str
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    fallback_count: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    total_duration: float = 0.0
    average_latency: float = 0.0
    success_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "total_requests": self.total_requests,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "fallback_count": self.fallback_count,
            "total_tokens": self.total_tokens,
            "total_cost": round(self.total_cost, 6),
            "average_latency": round(self.average_latency, 3),
            "success_rate": round(self.success_rate, 1),
        }


@dataclass
class BenchmarkReport:
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    task_success_rate: float = 0.0
    total_nodes: int = 0
    completed_nodes: int = 0
    failed_nodes: int = 0
    node_success_rate: float = 0.0
    repair_attempts: int = 0
    successful_repairs: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    average_task_duration: float = 0.0
    failure_categories: dict[str, int] = field(default_factory=dict)
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    validation_pass_rates: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tasks": self.total_tasks,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "task_success_rate": round(self.task_success_rate, 1),
            "total_nodes": self.total_nodes,
            "completed_nodes": self.completed_nodes,
            "failed_nodes": self.failed_nodes,
            "node_success_rate": round(self.node_success_rate, 1),
            "repair_attempts": self.repair_attempts,
            "successful_repairs": self.successful_repairs,
            "total_tokens": self.total_tokens,
            "total_cost": round(self.total_cost, 6),
            "average_task_duration": round(self.average_task_duration, 2),
            "failure_categories": self.failure_categories,
            "models": self.models,
            "validation_pass_rates": self.validation_pass_rates,
        }


class TelemetryCollector:
    """Collects and computes agent benchmarking and reliability telemetry."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        try:
            with connect(self.db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS telemetry_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT,
                        metric_type TEXT NOT NULL,
                        name TEXT NOT NULL,
                        value REAL NOT NULL,
                        tags TEXT,
                        created_at TEXT NOT NULL
                    );
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_task ON telemetry_metrics(task_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_metric ON telemetry_metrics(metric_type, name);")
        except Exception:
            pass

    def record_metric(
        self,
        name: str,
        value: float,
        metric_type: str = "counter",
        task_id: str | None = None,
        tags: dict[str, Any] | None = None,
    ) -> None:
        try:
            with connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO telemetry_metrics (task_id, metric_type, name, value, tags, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        metric_type,
                        name,
                        float(value),
                        json.dumps(tags or {}, default=str),
                        _now(),
                    ),
                )
        except Exception:
            pass

    def generate_report(self) -> BenchmarkReport:
        """Compute consolidated benchmark report across tasks, subtasks, events, and usage."""
        report = BenchmarkReport()

        try:
            with connect(self.db_path) as conn:
                # 1. Task metrics
                tasks = conn.execute("SELECT id, status, created_at, updated_at FROM tasks").fetchall()
                report.total_tasks = len(tasks)
                report.completed_tasks = sum(1 for t in tasks if str(t["status"]).upper() == "COMPLETED")
                report.failed_tasks = sum(1 for t in tasks if str(t["status"]).upper() in ("FAILED", "CANCELLED"))
                if report.total_tasks > 0:
                    report.task_success_rate = (report.completed_tasks / report.total_tasks) * 100.0

                # 2. Subtask / Node metrics
                subtasks = conn.execute("SELECT id, task_id, status, attempts FROM subtasks").fetchall()
                report.total_nodes = len(subtasks)
                report.completed_nodes = sum(1 for s in subtasks if str(s["status"]).upper() == "COMPLETED")
                report.failed_nodes = sum(1 for s in subtasks if str(s["status"]).upper() in ("FAILED", "CANCELLED", "BLOCKED"))
                if report.total_nodes > 0:
                    report.node_success_rate = (report.completed_nodes / report.total_nodes) * 100.0

                # 3. Events for repairs and validations
                events = conn.execute("SELECT event_type, payload FROM task_events").fetchall()
                validation_counts: dict[str, dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0})
                failure_cats: dict[str, int] = defaultdict(int)

                for ev in events:
                    etype = ev["event_type"]
                    payload_str = ev["payload"]
                    payload = {}
                    if payload_str:
                        try:
                            payload = json.loads(payload_str)
                        except Exception:
                            pass

                    if etype in ("NODE_RETRYING", "dag.node.retrying", "REPAIR_ATTEMPT"):
                        report.repair_attempts += 1
                    elif etype in ("NODE_COMPLETED", "dag.node.completed") and payload.get("attempts", 1) > 1:
                        report.successful_repairs += 1

                    if etype == "VALIDATION_CHECK" and isinstance(payload, dict):
                        chk_name = payload.get("name", "unknown")
                        status = payload.get("status", "")
                        if status == "PASS":
                            validation_counts[chk_name]["pass"] += 1
                        elif status in ("FAIL", "ERROR"):
                            validation_counts[chk_name]["fail"] += 1
                            failure_cats[chk_name.upper()] += 1

                for chk, counts in validation_counts.items():
                    tot = counts["pass"] + counts["fail"]
                    if tot > 0:
                        report.validation_pass_rates[chk] = round((counts["pass"] / tot) * 100.0, 1)

                report.failure_categories = dict(failure_cats)

        except Exception:
            pass

        # 4. Usage tracker stats
        from qz_core.executor import USAGE_TRACKER
        if USAGE_TRACKER:
            usage_sum = USAGE_TRACKER.summary()
            report.total_tokens = usage_sum.get("total_tokens", 0)
            report.total_cost = usage_sum.get("estimated_cost", 0.0)

            # Model comparison breakdown
            model_stats: dict[str, dict[str, Any]] = {}
            events_list = list(USAGE_TRACKER._events)
            by_m: dict[str, list[dict]] = defaultdict(list)
            for e in events_list:
                by_m[e.get("model", "unknown")].append(e)

            for m_name, m_events in by_m.items():
                tot_req = len(m_events)
                succ = sum(1 for e in m_events if e.get("success"))
                failed = tot_req - succ
                fallbacks = sum(1 for e in m_events if e.get("fallback_from"))
                m_tokens = sum(int(e.get("total_tokens") or 0) for e in m_events)
                m_cost = sum(float(e.get("estimated_cost") or 0.0) for e in m_events)
                m_dur = sum(float(e.get("duration") or 0.0) for e in m_events)
                avg_lat = m_dur / tot_req if tot_req > 0 else 0.0
                rate = (succ / tot_req * 100.0) if tot_req > 0 else 0.0

                stats = ModelBenchmarkStats(
                    model=m_name,
                    provider=m_events[0].get("provider") or m_name.split("/")[0],
                    total_requests=tot_req,
                    successful_requests=succ,
                    failed_requests=failed,
                    fallback_count=fallbacks,
                    total_tokens=m_tokens,
                    total_cost=m_cost,
                    total_duration=m_dur,
                    average_latency=avg_lat,
                    success_rate=rate,
                )
                model_stats[m_name] = stats.to_dict()

            report.models = model_stats

        return report


_telemetry_collector: TelemetryCollector | None = None


def get_telemetry_collector(db_path: str | Path | None = None) -> TelemetryCollector:
    global _telemetry_collector
    if _telemetry_collector is None:
        _telemetry_collector = TelemetryCollector(db_path=db_path)
    return _telemetry_collector
