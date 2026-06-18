"""Execution-order + transition-graph helpers for :class:`TemplateEngine`."""

from typing import Dict, List, Set

from ._models import PipelineTemplate


class _DagMixin:
    """Topological execution ordering and transition-graph analysis."""

    def get_execution_order(self, template: PipelineTemplate) -> List[List[str]]:
        """Compute execution order respecting dependencies.

        Uses Kahn's algorithm (BFS topological sort) to group phases into
        *waves*.  All phases in the same wave are independent and could run
        in parallel; the sequencer executes them serially for MVP.

        Returns:
            List of waves, each wave being a sorted list of phase IDs.
            E.g. [["research"], ["write"], ["fact_check"], ["apply_fixes"], ["final_output"]]

        Raises:
            ValueError: If a cycle is detected (returned as empty list from this
                        method — call validate_template() to get the error message).
        """
        phase_ids = {phase.id for phase in template.phases}

        # in_degree counts unsatisfied dependencies for each phase
        in_degree: Dict[str, int] = {phase.id: 0 for phase in template.phases}
        # dependents[x] = list of phases that must wait for x to finish
        dependents: Dict[str, List[str]] = {phase.id: [] for phase in template.phases}

        for phase in template.phases:
            for dep in phase.depends_on:
                if dep in phase_ids:
                    in_degree[phase.id] += 1
                    dependents[dep].append(phase.id)
                # Unknown deps are silently ignored here; validate_template() catches them

        # Start with phases that have no unsatisfied dependencies
        current_wave = sorted(pid for pid, deg in in_degree.items() if deg == 0)
        waves: List[List[str]] = []

        while current_wave:
            waves.append(current_wave)
            next_wave: List[str] = []
            for phase_id in current_wave:
                for dep_id in dependents[phase_id]:
                    in_degree[dep_id] -= 1
                    if in_degree[dep_id] == 0:
                        next_wave.append(dep_id)
            current_wave = sorted(next_wave)

        return waves

    # ------------------------------------------------------------------
    # Transition graph helpers (Issue #232)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_effective_transitions(
        template: "PipelineTemplate",
    ) -> Dict[str, Dict[str, str]]:
        """Compute effective transitions per phase using per-key merge semantics.

        Effective = {**template.default_transitions, **phase.transitions}

        Phase-level keys override pipeline-level defaults; absent keys fall
        back to the pipeline default.  This implements Rule 2 (per-key merge,
        not all-or-nothing replacement).

        Args:
            template: Loaded :class:`PipelineTemplate`.

        Returns:
            Mapping of phase_id → effective transitions dict.
        """
        result: Dict[str, Dict[str, str]] = {}
        for phase in template.phases:
            effective = {**template.default_transitions, **phase.transitions}
            result[phase.id] = effective
        return result

    @staticmethod
    def _detect_transition_cycles(  # noqa: C901
        effective_transitions: Dict[str, Dict[str, str]],
        all_phase_ids: Set[str],
    ) -> List[List[str]]:
        """Detect cycles in the transition graph using recursive DFS.

        Args:
            effective_transitions: Mapping of phase_id → effective transitions
                                   (from :meth:`_compute_effective_transitions`).
            all_phase_ids: Full set of known phase IDs.

        Returns:
            A list of cycles, each expressed as an ordered list of phase IDs
            forming the cycle (the last element loops back to the first).
            Returns an empty list when the transition graph is acyclic.
        """
        # Build adjacency: phase_id → sorted set of reachable phase_ids
        graph: Dict[str, List[str]] = {pid: [] for pid in all_phase_ids}
        for pid, eff in effective_transitions.items():
            for target in eff.values():
                if target in all_phase_ids and target not in graph[pid]:
                    graph[pid].append(target)
        for adj in graph.values():  # PLC0206: sort each adjacency list in place
            adj.sort()

        visited: Set[str] = set()
        rec_stack: Set[str] = set()
        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)
            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in rec_stack:
                    # Found cycle — extract cycle portion
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycles.append(cycle)
            path.pop()
            rec_stack.discard(node)

        for phase_id in sorted(all_phase_ids):
            if phase_id not in visited:
                dfs(phase_id, [])

        return cycles
