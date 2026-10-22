"""Registered fixed parallel workflows (RM15).

The prototype supports one trusted workflow with independent read-only
branches. A skill manifest may name only an ID listed here. The
workflow itself is run by the orchestrator (``local_loop``; spec 0.25
section 4.4, issue #30).

This set lists the workflows that exist, not the ones that run. Section
4.4 (spec 0.6): operator configuration names the enabled workflow ID,
the selected skill's manifest must declare it, and the effective
execution mode must admit it. The route decision validator checks all
three through RoutingContext (``parallel_workflows`` from the loaded
skills, ``enabled_workflows`` from configuration). A skill that
declares a known workflow which is not enabled still loads, and the
router sends it external with reason parallel_workflow.
"""

PARALLEL_WORKFLOWS: frozenset[str] = frozenset({"health-and-diagnostic-checklist"})
