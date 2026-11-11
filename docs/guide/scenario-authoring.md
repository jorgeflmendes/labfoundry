# Scenario authoring

Scenario YAML describes deterministic infrastructure and **generic actions**, not application-specific
integrations or GNS3 REST calls.

## Authoring principles

- use `ip: auto` when a concrete address is not pedagogically meaningful;
- use `ip: dhcp` only where an external DHCP domain such as the explicit GNS3 NAT uplink should own
  addressing;
- refer to scenario nodes/aliases instead of copying procedural IPs into scripts;
- put one-off behavior in `events` and finite recurring behavior in `traffic`; choose a distribution that matches the activity rather than defaulting to a fixed interval;
- use `guest.exec`/`guest.start` to run real software rather than adding protocol-specific core actions;
- declare captures independently of actions;
- treat `(scenario content, seed)` as part of the run identity.

## Validation first

```bash
labfoundry validate scenario.yaml
```

Schema/cross-reference/compilation errors are raised before contacting GNS3. `labfoundry run` adds a
runtime preflight for guest profiles, sandboxed upload/download paths and infrastructure actions
before project deployment.

## Procedural context

Guest commands receive deterministic context variables such as `LABFOUNDRY_NODE`,
`LABFOUNDRY_NODE_IP`, `LABFOUNDRY_TARGET` and `LABFOUNDRY_TARGET_IP`. This allows reusable scripts to
follow a seed-selected actor or target without hardcoded addresses.

## Long-running activity

Use `guest.start` for activity generators that should continue while the scenario clock advances and
`guest.stop` when an explicit stop point is useful. The orchestrator also performs best-effort process
tree cleanup at scenario end/failure.

## Determinism versus external services

LabFoundry can provide explicit NAT egress, but public sites are not deterministic dependencies.
Prefer controlled services when exact replay matters; use public services when real-world traffic
variation is part of the exercise.
