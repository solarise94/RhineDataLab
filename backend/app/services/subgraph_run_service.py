from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from fastapi import HTTPException

from app.models.cards import Card
from app.models.graph import GraphState, RunRecord
from app.services.dependency_attention_service import DependencyAttentionService
from app.services.project_service import ProjectService
from app.services.utils import utc_now


logger = logging.getLogger(__name__)


class SubgraphRunService:
    """Schedule and execute a subgraph of cards in topological order.

    This is the first increment of docs/66 §4 (form 1 only: ``from_card``).
    It builds a card closure starting from ``start_card_id``, topologically
    sorts the closure by producer-consumer dependencies, then runs the cards
    in order. Runs are persisted so the UI can show progress; restart recovery
    is intentionally out of scope for this increment (see docs/69 decision 1).
    """

    BATCH_STATUS_RUNNING = "running"
    BATCH_STATUS_COMPLETED = "completed"
    BATCH_STATUS_FAILED = "failed"
    BATCH_STATUS_STOPPED = "stopped"

    def __init__(self, project_service: ProjectService, worker_service: Any) -> None:
        self.project_service = project_service
        self.worker_service = worker_service

    def start_from_card(
        self,
        project_id: str,
        start_card_id: str,
        *,
        worker_type: str | None = None,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
        propagate: str = "all",
        stop_on_fail: bool = True,
    ) -> dict[str, Any]:
        """Start a form-1 subgraph run from ``start_card_id`` to all downstream cards."""
        lock = self.project_service.lock_for(project_id)
        with lock:
            store = self.project_service.graph_store(project_id)
            cards = store.load_cards()
            graph = store.load_graph()
            card_by_id = {card.card_id: card for card in cards}
            if start_card_id not in card_by_id:
                raise HTTPException(status_code=404, detail=f"Card not found: {start_card_id}")

            planned_cards = self._compute_subgraph_cards(cards, graph, start_card_id)
            if not planned_cards:
                raise HTTPException(status_code=409, detail="Subgraph contains no runnable cards.")

            batch_run_id = self._new_batch_run_id(graph)
            batch_state = {
                "batch_run_id": batch_run_id,
                "mode": "from_card",
                "start_card_id": start_card_id,
                "status": self.BATCH_STATUS_RUNNING,
                "planned_cards": planned_cards,
                "completed_cards": [],
                "failed_cards": [],
                "skipped_cards": [],
                "card_run_ids": {},
                "propagate": propagate,
                "stop_on_fail": stop_on_fail,
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
            self._save_batch_state(graph, batch_state)
            store.save_graph(graph)

        thread = threading.Thread(
            target=self._run_subgraph_batch,
            args=(project_id, batch_run_id),
            kwargs={
                "worker_type": worker_type,
                "profile_id": profile_id,
                "python_runtime": python_runtime,
                "r_runtime": r_runtime,
            },
            daemon=True,
        )
        thread.start()

        return {
            "batch_run_id": batch_run_id,
            "planned_cards": planned_cards,
            "status": self.BATCH_STATUS_RUNNING,
            "start_card_id": start_card_id,
        }

    def get_batch_state(self, project_id: str, batch_run_id: str) -> dict[str, Any] | None:
        store = self.project_service.graph_store(project_id)
        graph = store.load_graph()
        batches = self._load_batch_runs(graph)
        return batches.get(batch_run_id)

    def _compute_subgraph_cards(self, cards: list[Card], graph: GraphState, start_card_id: str) -> list[str]:
        """Return topologically sorted card ids for the form-1 subgraph.

        The result includes ``start_card_id`` followed by all downstream cards
        in dependency order. Cycles are detected and raised as HTTPException.
        """
        consumers_by_card = DependencyAttentionService.build_consumer_edges(cards, graph)

        # BFS downstream to collect closure.
        closure: set[str] = {start_card_id}
        queue: deque[str] = deque([start_card_id])
        while queue:
            current = queue.popleft()
            for downstream in consumers_by_card.get(current, set()):
                if downstream in closure:
                    continue
                closure.add(downstream)
                queue.append(downstream)

        if not closure:
            return []

        # Build dependency edges within the closure: card -> set of predecessor cards.
        predecessors: dict[str, set[str]] = {card_id: set() for card_id in closure}
        for producer, consumers in consumers_by_card.items():
            if producer not in closure:
                continue
            for consumer in consumers:
                if consumer in closure:
                    predecessors[consumer].add(producer)

        # Kahn's algorithm for topological sort.
        in_degree = {card_id: len(preds) for card_id, preds in predecessors.items()}
        ready = sorted(card_id for card_id, degree in in_degree.items() if degree == 0)
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for consumer in sorted(consumers_by_card.get(current, set())):
                if consumer not in closure:
                    continue
                in_degree[consumer] -= 1
                if in_degree[consumer] == 0:
                    ready.append(consumer)
                    ready.sort()

        if len(ordered) != len(closure):
            remaining = closure - set(ordered)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Dependency cycle detected in subgraph; cannot determine execution order.",
                    "error_code": "subgraph_cycle",
                    "card_ids": sorted(remaining),
                },
            )

        return ordered

    def _run_subgraph_batch(
        self,
        project_id: str,
        batch_run_id: str,
        *,
        worker_type: str | None = None,
        profile_id: str | None = None,
        python_runtime: str | None = None,
        r_runtime: str | None = None,
    ) -> None:
        """Background scheduler for a subgraph batch run.

        Runs cards in topological order, respecting executor concurrency limits
        by retrying on capacity-full 409s. Persists progress after every state
        change. Stops on first failure when ``stop_on_fail`` is True.
        """
        pending_card_ids: set[str] = set()
        running_card_runs: dict[str, str] = {}  # card_id -> run_id
        completed_card_ids: set[str] = set()
        failed_card_ids: set[str] = set()
        skipped_card_ids: set[str] = set()
        card_run_ids: dict[str, str] = {}
        stop_on_fail = True
        propagate = "all"

        def load_state() -> dict[str, Any] | None:
            return self.get_batch_state(project_id, batch_run_id)

        def save_state() -> None:
            lock = self.project_service.lock_for(project_id)
            with lock:
                store = self.project_service.graph_store(project_id)
                graph = store.load_graph()
                batch_state = self._load_batch_runs(graph).get(batch_run_id)
                if batch_state is None:
                    return
                batch_state["completed_cards"] = sorted(completed_card_ids)
                batch_state["failed_cards"] = sorted(failed_card_ids)
                batch_state["skipped_cards"] = sorted(skipped_card_ids)
                batch_state["card_run_ids"] = dict(card_run_ids)
                batch_state["updated_at"] = utc_now()
                self._save_batch_state(graph, batch_state)
                store.save_graph(graph)

        state = load_state()
        if state is None:
            logger.error("Batch run %s not found for project %s; aborting scheduler.", batch_run_id, project_id)
            return

        planned_cards = list(state["planned_cards"])
        stop_on_fail = bool(state.get("stop_on_fail", True))
        propagate = str(state.get("propagate", "all"))

        # Build predecessor map within the planned set.
        store = self.project_service.graph_store(project_id)
        cards = store.load_cards()
        graph = store.load_graph()
        consumers_by_card = DependencyAttentionService.build_consumer_edges(cards, graph)
        predecessors: dict[str, set[str]] = {card_id: set() for card_id in planned_cards}
        for producer, consumers in consumers_by_card.items():
            if producer not in set(planned_cards):
                continue
            for consumer in consumers:
                if consumer in set(planned_cards):
                    predecessors[consumer].add(producer)

        pending_card_ids = set(planned_cards)

        try:
            while pending_card_ids or running_card_runs:
                # Determine ready cards: all predecessors completed.
                ready = sorted(
                    card_id
                    for card_id in pending_card_ids
                    if predecessors[card_id].issubset(completed_card_ids)
                )

                # Try to start ready cards.
                for card_id in ready:
                    if card_id in running_card_runs:
                        continue
                    try:
                        if card_id == planned_cards[0]:
                            # Start card is rerun; downstream is handled by the scheduler.
                            response = self.worker_service.rerun_card(
                                project_id,
                                card_id,
                                worker_type=worker_type,
                                profile_id=profile_id,
                                python_runtime=python_runtime,
                                r_runtime=r_runtime,
                                propagate="none",
                            )
                        else:
                            response = self.worker_service.start_run(
                                project_id,
                                card_id,
                                worker_type=worker_type,
                                profile_id=profile_id,
                                python_runtime=python_runtime,
                                r_runtime=r_runtime,
                                propagate_invalidation=True,
                                batch_run_id=batch_run_id,
                            )
                        run_id = response.get("run_id")
                        if run_id:
                            run_id_str = str(run_id)
                            running_card_runs[card_id] = run_id_str
                            card_run_ids[card_id] = run_id_str
                            pending_card_ids.discard(card_id)
                    except HTTPException as exc:
                        if exc.status_code == 409:
                            detail = exc.detail
                            error_code = None
                            if isinstance(detail, dict):
                                error_code = detail.get("error_code")
                            if error_code == "executor_capacity_full":
                                # Retry later; do not mark failed.
                                continue
                            # Other 409 means the card cannot start (input resolution, active run, etc.).
                            logger.warning(
                                "Subgraph batch %s card %s cannot start: %s",
                                batch_run_id,
                                card_id,
                                detail,
                            )
                            failed_card_ids.add(card_id)
                            pending_card_ids.discard(card_id)
                            if stop_on_fail:
                                skipped = pending_card_ids - set(running_card_runs.keys())
                                skipped_card_ids.update(skipped)
                                pending_card_ids.clear()
                                break
                        else:
                            raise

                if not running_card_runs and not pending_card_ids:
                    break
                if not running_card_runs and pending_card_ids:
                    # No cards running but still pending means every pending card
                    # hit a non-capacity 409. Stop to avoid spinning.
                    skipped_card_ids.update(pending_card_ids)
                    pending_card_ids.clear()
                    break

                save_state()

                # Poll running cards until at least one reaches terminal status.
                terminal_reached = False
                while running_card_runs and not terminal_reached:
                    time.sleep(1.0)
                    store = self.project_service.graph_store(project_id)
                    graph = store.load_graph()
                    runs_by_id = {run.run_id: run for run in graph.runs}
                    terminal_statuses = {"success", "failed", "cancelled", "reviewed", "needs_review"}
                    for card_id, run_id in list(running_card_runs.items()):
                        run = runs_by_id.get(run_id)
                        if run is None:
                            # Run record missing; treat as failed.
                            failed_card_ids.add(card_id)
                            del running_card_runs[card_id]
                            terminal_reached = True
                            continue
                        if run.status in terminal_statuses:
                            if run.status in {"failed", "cancelled", "needs_review"}:
                                failed_card_ids.add(card_id)
                                if stop_on_fail:
                                    skipped = pending_card_ids - set(running_card_runs.keys())
                                    skipped_card_ids.update(skipped)
                                    pending_card_ids.clear()
                            else:
                                completed_card_ids.add(card_id)
                            del running_card_runs[card_id]
                            terminal_reached = True

                if stop_on_fail and failed_card_ids and not pending_card_ids:
                    break

            # Finalize state.
            lock = self.project_service.lock_for(project_id)
            with lock:
                store = self.project_service.graph_store(project_id)
                graph = store.load_graph()
                batch_state = self._load_batch_runs(graph).get(batch_run_id)
                if batch_state is not None:
                    if failed_card_ids or skipped_card_ids:
                        batch_state["status"] = self.BATCH_STATUS_FAILED
                    else:
                        batch_state["status"] = self.BATCH_STATUS_COMPLETED
                    batch_state["completed_cards"] = sorted(completed_card_ids)
                    batch_state["failed_cards"] = sorted(failed_card_ids)
                    batch_state["skipped_cards"] = sorted(skipped_card_ids)
                    batch_state["card_run_ids"] = dict(card_run_ids)
                    batch_state["updated_at"] = utc_now()
                    self._save_batch_state(graph, batch_state)
                    store.save_graph(graph)
        except Exception:
            logger.exception("Subgraph batch %s scheduler failed for project %s", batch_run_id, project_id)
            lock = self.project_service.lock_for(project_id)
            with lock:
                store = self.project_service.graph_store(project_id)
                graph = store.load_graph()
                batch_state = self._load_batch_runs(graph).get(batch_run_id)
                if batch_state is not None:
                    batch_state["status"] = self.BATCH_STATUS_FAILED
                    batch_state["updated_at"] = utc_now()
                    self._save_batch_state(graph, batch_state)
                    store.save_graph(graph)

    def _load_batch_runs(self, graph: GraphState) -> dict[str, dict[str, Any]]:
        if not isinstance(graph.metadata, dict):
            graph.metadata = {}
        return graph.metadata.setdefault("batch_runs", {})

    def _save_batch_state(self, graph: GraphState, batch_state: dict[str, Any]) -> None:
        batches = self._load_batch_runs(graph)
        batches[batch_state["batch_run_id"]] = batch_state

    def _new_batch_run_id(self, graph: GraphState) -> str:
        existing = set(self._load_batch_runs(graph).keys())
        import uuid

        while True:
            candidate = f"batch_{uuid.uuid4().hex[:12]}"
            if candidate not in existing:
                return candidate
