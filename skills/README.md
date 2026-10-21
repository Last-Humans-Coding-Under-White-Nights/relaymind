# Demo skill packages

One directory per skill. Each package contains a `SKILL.md` with Agent
Skills `name` and `description` frontmatter, plus a registry manifest
`skill.manifest.json` with RelayMind-specific settings (version,
languages, local eligibility, allowed tool IDs, resource paths, completion
predicate ID). This directory is the default skill root; operators can
configure others. See [`docs/SPECIFICATION.md`](../docs/SPECIFICATION.md)
sections 4.2 and 9.

Planned demo skills:

| Skill | Tools | Expected route |
|-------|-------|----------------|
| check-service-health | get_service_health | Local |
| lookup-latest-deployment | get_latest_deployment | Local |
| summarize-incident-counts | get_incident_counts | Local |
| investigate-performance | get_latest_deployment, get_metrics, get_recent_logs | Local prep or escalation |
| delete-demo-database | list_demo_databases, delete_demo_database | Confirmation required |
| health-and-diagnostic-checklist | get_service_health | Parallel demo workflow |

Each skill ships paired English and Chinese examples and completion
criteria.
