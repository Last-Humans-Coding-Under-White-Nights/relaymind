# Demo skill packages

One directory per skill. This directory is the default skill root,
`skills/`, a path relative to the working directory, so the gateway
finds it when it starts in the checkout root; started anywhere else,
or installed as a package, it needs configured roots. Operators can
configure an ordered list of local roots instead, which replaces the
default (spec section 4.2). Every subdirectory of a root is
a candidate package, a missing or invalid `SKILL.md` or
`skill.manifest.json` stops the gateway from starting, and skill names
must be unique across roots. See
[`docs/SPECIFICATION.md`](../docs/SPECIFICATION.md) sections 4.2 and 9.

Each package contains:

- `SKILL.md`: Agent Skills `name` and `description` frontmatter, then the
  instruction body with steps, rules, and completion criteria. The
  frontmatter `name` must match the directory name. An `allowed-tools`
  line here grants nothing.
- `skill.manifest.json`: the registry manifest, validated against
  `src/relaymind/skills/schemas/skill_manifest.v1.json`. It holds the
  version, languages, local eligibility, allowed tool IDs, resource
  paths, the completion predicate ID, and the optional parallel-workflow
  ID. Tool IDs, predicate IDs, and workflow IDs must be registered in the
  gateway. `local_eligible: false` marks a skill external-required
  (RM04): its final answer comes from an external model. Declaring a
  parallel-workflow ID only joins the workflow; operator configuration
  must also enable it, and the effective execution mode must admit it
  (spec section 4.4).
- `examples.json`: paired English and Chinese examples. They are
  illustrative, use the sandbox fixture values, and name the tool calls
  they assume. A test runs those calls against the sandbox and checks
  each response with the skill's completion predicate.

| Skill | Tools | Expected route | local_eligible | Completion predicate |
|-------|-------|----------------|----------------|----------------------|
| check-service-health | get_service_health | Local | true | health_status_preserved |
| lookup-latest-deployment | get_latest_deployment | Local | true | deployment_reported |
| summarize-incident-counts | get_incident_counts | Local | true | incident_counts_reported |
| investigate-performance | get_latest_deployment, get_metrics, get_recent_logs | Local prep or escalation, external assessment | false | performance_evidence_separated |
| delete-demo-database | list_demo_databases, delete_demo_database | Confirmation required | true | deletion_outcome_reported |
| health-and-diagnostic-checklist | get_service_health | Parallel demo workflow, external synthesis | false | health_checklist_separated |

Changing a package changes its content hash; bump `version` in
`skill.manifest.json` as well (RM07).

The Chinese examples have not had native review yet. Spec section 9
requires it for the evaluation corpus.
