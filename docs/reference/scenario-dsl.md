# Scenario DSL reference

Scenario documents use language version `0.2`. The schema is intentionally strict: unknown fields are rejected, validated models are frozen, network segments must not overlap, and cross-references are checked before deployment.

The normative implementation is [`labfoundry.schema.scenario`][labfoundry.schema.scenario]. This page is the human-oriented field reference.

## Top-level document

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `version` | literal `"0.2"` | no | `"0.2"` | DSL version. |
| `scenario` | object | yes | — | Scenario identity and deterministic root seed. |
| `clock` | object | yes | — | Scenario start time and time scaling. |
| `network` | object | yes | — | Logical IPv4 segments and GNS3-backed node groups. |
| `traffic` | object | no | empty profiles/assignments | Recurring background traffic. |
| `background_traffic` | object | no | `null` | Seeded guest activity inserted into warped gaps. |
| `provision` | list | no | `[]` | Out-of-band guest setup executed before packet capture starts. |
| `events` | list | no | `[]` | One-off semantic timeline events. |
| `captures` | list | no | `[]` | Packet-capture observation points. |
| `evidence` | object | no | audit enabled | Evidence-export and realism-audit policy. |

## Identifiers

Logical identifiers use the public `Identifier` type. They must start with an ASCII letter and may then contain letters, digits, `_`, `.`, or `-`.

Examples:

```text
ws
ws.finance
branch-01
router_core
```

## `scenario`

```yaml
scenario:
  id: network-case-001
  seed: 834927
```

| Field | Type | Required | Constraints |
| --- | --- | --- | --- |
| `id` | `Identifier` | yes | See [Identifiers](#identifiers). |
| `seed` | integer | yes | `0 <= seed <= 2^63 - 1`. |

The seed is the deterministic root from which LabFoundry derives namespaced sub-seeds for procedural choices.

## `clock`

```yaml
clock:
  start: "2025-04-17T08:00:00+01:00"
  speed: 1.0
  duration: 2h
  warp_gaps: false
  sync_guests: false
  settle_delay: 3s
```

| Field | Type | Required | Default | Constraints |
| --- | --- | --- | --- | --- |
| `start` | timezone-aware datetime | yes | — | Must include an explicit timezone offset. |
| `speed` | float | no | `1.0` | `0 < speed <= 10000`. |
| `duration` | duration string or null | no | `null` | Positive scenario duration; required when recurring `traffic` exists. Cannot be combined with `warp_gaps`. |
| `warp_gaps` | boolean | no | `false` | When true, warps large idle intervals between event batches while running bursts at 1.0x line rate. Requires `speed: 1.0` and cannot be combined with `duration`. |
| `sync_guests` | boolean | no | `false` | Set guest clocks after networking, again after provision and identity checks, and after each warped gap. |
| `settle_delay` | duration string | no | `3s` | Settle delay to let in-flight packets land before warping scenario anchor. |

`speed` scales scheduling delays and retimed packet timestamps. A value of `10` maps one real
elapsed second to ten scenario seconds. Guest protocol timers and commands still run in real time;
guest clocks are not accelerated. `warp_gaps` skips idle intervals while leaving active segments
at 1.0x. It requires events to finish before the next batch and waits for Linux pending connections
before jumping. Reading, service delays and other command work still take real time.
See the [time model](../advanced/architecture.md#time-model) for ordering and clock anchors.

## `network`

```yaml
network:
  segments:
    users:
      subnet: 10.10.10.0/24
  nodes: {}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `segments` | mapping of `Identifier -> SegmentSpec` | yes | Logical IPv4 broadcast domains. |
| `nodes` | mapping of `Identifier -> NodeSpec` | yes | Logical node groups backed by GNS3 templates. |
| `egress` | `EgressSpec` or null | no | Explicit GNS3 NAT attachment for real-world outbound connectivity. |
| `allow_cloud_nodes` | boolean | no | Default `false`; must be explicitly enabled before Cloud templates are accepted. |

All segment CIDRs must be mutually non-overlapping.

### Segment

```yaml
segments:
  users:
    subnet: 10.10.10.0/24
```

| Field | Type | Required |
| --- | --- | --- |
| `subnet` | IPv4 network/CIDR | yes |

### Node group

```yaml
nodes:
  ws:
    template: "RF Ubuntu Client VBox"
    template_type: virtualbox
    count: 3
    interfaces:
      eth0:
        segment: users
        ip: auto
        adapter: 0
        port: 0
```

| Field | Type | Required | Default | Constraints / meaning |
| --- | --- | --- | --- | --- |
| `template` | string | yes | — | Non-empty GNS3 template name. |
| `template_type` | enum or null | no | `null` | Expected GNS3 template type; used as a preflight assertion when supplied. |
| `count` | integer | no | `1` | `1..1000`; values greater than one expand the group into numbered nodes. |
| `interfaces` | mapping | no | `{}` | Logical interfaces attached to segments. |
| `guest_profile` | `Identifier` or null | no | `null` | Name of an out-of-band profile in the separate guest inventory. |
| `ipv4_forwarding` | boolean | no | `false` | Ask guest provisioning to enable IPv4 forwarding. |

Supported `template_type` literals are:

```text
virtualbox, incus, docker, vpcs, dynamips, iou, vmware,
traceng, cloud, ethernet_switch, ethernet_hub
```

`incus` selects a system container in a configured [scenario environment](../guide/incus.md);
its `template` is a local container image alias or fingerprint and `guest_profile` is required.
Other values assert GNS3 template types, not a list of tested hypervisors. The VM execution path
uses VirtualBox. The `docker` type refers to a GNS3 template. GNS3 capabilities and resolved
template types are checked separately at backend preflight.
NAT nodes must use `network.egress`; NAT templates are rejected in `network.nodes` even when
`template_type` is omitted.

When `count > 1`, LabFoundry expands a group deterministically, for example `ws` with `count: 3` becomes `ws-01`, `ws-02`, `ws-03`.

### Interface

| Field | Type | Required | Default | Constraints / meaning |
| --- | --- | --- | --- | --- |
| `segment` | `Identifier` | yes | — | Must name an existing `network.segments` entry. |
| `ip` | `auto`, `dhcp`, or IPv4 interface | no | `auto` | Explicit addresses include prefix length, e.g. `10.10.10.5/24`; `dhcp` leaves addressing to the guest network. |
| `mac` | string or null | no | `null` | Optional hardware MAC address for guest physical realism spoofing. |
| `gateway` | IPv4 address or null | no | `null` | Optional default gateway for static guest provisioning; must belong to the segment. |
| `dns` | list/tuple of IPv4 addresses | no | `[]` | DNS resolvers written by guest provisioning. |
| `adapter` | integer | no | `0` | `0..35`. |
| `port` | integer | no | `0` | Must be non-negative. VirtualBox interfaces must use `port: 0`. |

A node may not reuse the same `(adapter, port)` pair for two interfaces.

`ip: auto` is resolved by deterministic IPAM. `ip: dhcp` is never allocated by deterministic IPAM and is intended for networks such as the explicit GNS3 NAT uplink. Explicit addresses are reserved before automatic allocation so generated addresses cannot steal a static assignment.

### Real-world egress

`egress` is deliberately explicit. Its `mode` is currently `nat`, `segment` names the logical segment that receives the built-in GNS3 NAT node, and `name` optionally changes the synthetic NAT-node name.

```yaml
network:
  segments:
    uplink:
      subnet: 192.168.122.0/24
  egress:
    mode: nat
    segment: uplink
    name: labfoundry-nat
  nodes: {}
```

The NAT node is created through the GNS3 Controller API. Public-service behavior itself remains ordinary guest execution rather than a LabFoundry-specific feature.


## `traffic`

```yaml
traffic:
  profiles:
    office-user:
      actions: []
  assignments:
    - profile: office-user
      nodes: ws
```

| Field | Type | Required | Default |
| --- | --- | --- | --- |
| `profiles` | mapping of `Identifier -> TrafficProfileSpec` | no | `{}` |
| `assignments` | list of `TrafficAssignmentSpec` | no | `[]` |

Every assignment must reference an existing profile and an existing node group.

### Traffic profile

```yaml
profiles:
  office-user:
    actions:
      - action: guest.exec
        to: web
        interval:
          distribution: uniform
          min: 10s
          max: 60s
        parameters:
          script: 'curl -fsS "http://$LABFOUNDRY_TARGET_IP/" >/dev/null'
```

A profile contains an `actions` list. Each action has:

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `action` | string | yes | — | Semantic action name interpreted by the traffic executor. |
| `interval` | object | yes | — | Recurrence distribution. |
| `to` | `Identifier` or null | no | `null` | Logical destination/group reference. |
| `parameters` | mapping | no | `{}` | Action-specific parameters. |

The schema deliberately keeps `action` and `parameters` extensible; semantic validation belongs to
the runtime layer. Recurring traffic supports every generic guest primitive, including `guest.exec`,
`guest.start`, `guest.stop`, file transfer, directory creation, process wait, and process status.
Use `guest.exec` for bounded application activity; use a stable process ID when recurring actions
operate on a tracked process. One-off events additionally support GNS3 infrastructure actions
documented in [Architecture](../advanced/architecture.md#infrastructure-actions).

### Traffic intervals

Exactly the fields for the selected distribution must be present.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `distribution` | distribution name | yes | Selects the recurrence model. |
| `value` | duration string or null | only for `fixed` | Fixed interval. |
| `min` | duration string or null | only for `uniform` | Inclusive lower interval bound used by the sampler. |
| `max` | duration string or null | only for `uniform` | Upper interval bound used by the sampler. |
| `mean` | duration string or null | for `exponential`, `normal` | Mean of the selected distribution. |
| `stddev` | duration string or null | only for `normal` | Standard deviation of the normal distribution. |
| `median` | duration string or null | only for `lognormal` | Median of the lognormal distribution. |
| `sigma` | float or null | only for `lognormal` | Shape of the lognormal distribution. |
| `scale` | duration string or null | for `gamma`, `weibull`, `pareto` | Time scale of the selected distribution. |
| `shape` | float or null | for `gamma`, `weibull`, `pareto` | Dimensionless shape of the selected distribution. |

=== "fixed"

    ```yaml
    interval:
      distribution: fixed
      value: 30s
    ```

=== "uniform"

    ```yaml
    interval:
      distribution: uniform
      min: 10s
      max: 90s
    ```

=== "exponential"

    ```yaml
    interval:
      distribution: exponential
      mean: 45s
    ```

=== "normal"

    ```yaml
    interval:
      distribution: normal
      mean: 45s
      stddev: 12s
    ```

=== "lognormal"

    ```yaml
    interval:
      distribution: lognormal
      median: 45s
      sigma: 0.6
    ```

=== "gamma"

    ```yaml
    interval:
      distribution: gamma
      shape: 2.0
      scale: 20s
    ```

=== "weibull"

    ```yaml
    interval:
      distribution: weibull
      shape: 1.5
      scale: 30s
    ```

=== "pareto"

    ```yaml
    interval:
      distribution: pareto
      shape: 2.5
      scale: 10s
    ```

Durations use `<number><unit>` with units `ms`, `s`, `m`, `h`, or `d`, for example `250ms`, `2s`, `15m`, `3h`, or `1.5d`.

### Traffic assignment

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `profile` | `Identifier` | yes | Existing profile name. |
| `nodes` | `Identifier` | yes | Existing node-group name. |

The compiler expands one deterministic recurring stream per concrete node/action combination.

## `background_traffic`

This section requires `clock.warp_gaps` and inserts bounded `guest.exec` events in long
gaps between scheduled events. It uses the scenario seed, so compiling the same YAML produces the
same event times and activity choices.

```yaml
background_traffic:
  min_gap: 30m
  min_interval: 12m
  max_interval: 25m
  max_idle_gap: 30m
  max_events_per_gap: 5
  activities:
    - from: ws
      to: web
      parameters:
        script: 'curl -fsS "http://$LABFOUNDRY_TARGET_IP/" >/dev/null'
        timeout: 30s
```

Only gaps between declared events at least `min_gap` long are considered. Event spacing is sampled
between `min_interval` and `max_interval`, with at most `max_events_per_gap` events in each gap.
Without `max_idle_gap`, each generated action stays at least `min_interval` from the next declared
event. `activities` accepts `guest.exec` parameters and singleton source and target node groups.
Generated events appear in the compiled plan
and ground truth. They execute in the guest and are captured like declared events. Warped timestamps
remain marked by the realism audit.
An activity can set `weight` (positive integer, default `1`) to change how often it is chosen.
Set `max_idle_gap` to require that no interval between declared and generated events exceeds
that duration. This permits an event closer to the next declared event; compilation fails if
`max_events_per_gap` cannot cover the interval.
`max_idle_gap` must be at least both `min_gap` and `max_interval`.
Each generated command receives `LABFOUNDRY_BACKGROUND_SEED` in its environment for reproducible
choices within the command.

## `provision`

`provision` contains generic guest actions that execute after guest networking is configured but
before any student-facing packet capture starts. It is intended for copying scripts, creating
directories, installing scenario-specific configuration, and other setup that should not contaminate
the evidence PCAPs.

```yaml
provision:
  - nodes: ws
    action: guest.upload
    parameters:
      source: scripts/office_worker.py
      destination: /opt/labfoundry/office_worker.py

  - nodes: ws
    action: guest.exec
    parameters:
      script: chmod 0755 /opt/labfoundry/office_worker.py
```

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `nodes` | `Identifier` | yes | — | Existing node group; the compiler expands the action to every concrete node in the group. |
| `action` | `guest.exec`, `guest.start`, `guest.upload`, `guest.copy`, or `guest.mkdir` | yes | — | Generic out-of-band setup primitive. |
| `to` | `Identifier` or null | no | `null` | Target guest for `guest.copy`; otherwise populates procedural target environment variables. |
| `parameters` | mapping | no | `{}` | Parameters accepted by the selected guest primitive. |

Provisioning is deterministic and ordered. It is deliberately separate from timeline `events`: setup
finishes before `captures` are started, while events are part of the observed scenario runtime.

## `events`

An event is a one-off semantic action at an absolute or relative point in scenario time.

```yaml
- id: suspicious-connect
  after:
    event: suspicious-dns
    delay: 2s
  action: guest.exec
  from: affected
  to: external
  parameters:
    script: 'curl -fsS "https://$LABFOUNDRY_TARGET_IP/" >/dev/null'
```

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `id` | `Identifier` or null | no | generated | Stable event identifier. Generated IDs are `event-0001`, `event-0002`, ... |
| `at` | string or null | exactly one of `at`/`after` | — | Absolute datetime or scenario-local time of day. |
| `after` | duration string or object | exactly one of `at`/`after` | — | Relative timing expression. |
| `select` | selector object or null | no | `null` | Deterministically select concrete nodes and bind an alias. |
| `when` | condition object or null | no | `null` | Gate this event on an earlier `guest.exec` result. |
| `action` | string | yes | — | Semantic action name. |
| `from` | `Identifier` or null | no | `null` | Source node/group/selector alias. Must resolve to exactly one node. |
| `to` | `Identifier` or null | no | `null` | Destination node/group/selector alias. Must resolve to exactly one node. |
| `parameters` | mapping | no | `{}` | Action-specific parameters. |

### Absolute event time with `at`

`at` accepts either an ISO datetime or a time of day:

```yaml
at: "2025-04-17T09:17:32+01:00"
```

```yaml
at: "09:17:32"
```

A timezone-less ISO datetime inherits the scenario timezone. A time of day uses the scenario start date; if that time would be before `clock.start`, it is interpreted as the next day.

### Relative event time with `after`

Relative to the immediately previous event:

```yaml
after: 8s
```

Relative times use the earlier event's scheduled timestamp, not its completion time. Without gap
warping, actions at different timestamps can overlap. Use `when` to depend on a command result.

The first event cannot use that form because no previous timestamp exists.

Relative to a named earlier event:

```yaml
after:
  event: suspicious-dns
  delay: 2s
```

The object form has two fields: optional `event` (a prior event ID) and required `delay` (a compact duration string).

`event` may be omitted in the object form, in which case the immediately previous event is the base. Named references must point to a prior event.

### Conditional events

`when` evaluates the result of an earlier `guest.exec` event. The source event must occur earlier
than the conditional event and should use `check: false` when a non-zero exit code is an expected
branch rather than a run failure. A non-matching condition skips the event; it does not fail the run.

```yaml
- id: service-probe
  at: "09:17:32"
  action: guest.exec
  from: affected
  to: external
  parameters:
    script: 'curl -fsS http://$LABFOUNDRY_TARGET_IP/health'
    check: false

- id: transfer
  after: 2s
  action: guest.exec
  from: affected
  to: external
  when:
    event: service-probe
    exit_code: 0
    stdout_matches: 'healthy|ready'
  parameters:
    script: 'curl -fsS http://$LABFOUNDRY_TARGET_IP/upload'
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `event` | `Identifier` | yes | Earlier `guest.exec` event whose command result is inspected. |
| `exit_code` | integer or null | no | Required command exit code. |
| `stdout_matches` | regular expression or null | no | Required match in command stdout. |
| `stderr_matches` | regular expression or null | no | Required match in command stderr. |

### Deterministic selectors

```yaml
select:
  from: ws
  count: 1
  as: affected
```

| Field | Type | Required | Constraints |
| --- | --- | --- | --- |
| `from` | `Identifier` | yes | Existing group or previously-created alias. |
| `count` | integer | no | Default `1`; must be >= 1 and <= available population. |
| `as` | `Identifier` | yes | Must not collide with an existing group/alias. |

Selection is deterministic for a fixed root seed, event ID, and alias name.

## Built-in 0.2 runtime actions

The end-to-end runner intentionally exposes general primitives:

- `guest.exec`, `guest.start`, `guest.stop`, `guest.status`, `guest.wait`, `guest.upload`, `guest.copy`, `guest.download`, `guest.mkdir`;
- `gns3.node.start`, `gns3.node.stop`, `gns3.node.restart`;
- `gns3.link.update`;
- `gns3.snapshot.create`, `gns3.snapshot.restore`, `gns3.snapshot.delete`.

Application behavior should normally be implemented by software/scripts inside guests rather than
new LabFoundry action types. See [Guest execution](../guide/guest-execution.md).

## `captures`

```yaml
captures:
  - name: users-perimeter
    format: pcapng
    timestamp_policy: preserve
    endpoints:
      - name: users
        node: gateway
        interface: users
        description: User network edge
      - name: wan
        node: gateway
        interface: wan
        description: Internet uplink
    metadata:
      comment: Combined perimeter evidence
      application: Wireshark
```

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `name` | `Identifier` | yes | — | Capture output identity. |
| `endpoint` | object or null | one endpoint form | `null` | Backwards-compatible single capture endpoint. |
| `endpoints` | list | PCAPNG multi-interface form | `[]` | One or more named capture interfaces. |
| `segments` | list of identifiers | PCAPNG multi-interface form | `[]` | Capture every compiled link attached to each named segment. |
| `data_link_type` | enum | no | `DLT_EN10MB` | GNS3 capture data-link type. |
| `format` | `pcap` or `pcapng` | no | `pcap` | Evidence container format. |
| `timestamp_policy` | `retime` or `preserve` | no | `retime` | Map timestamps to scenario time or retain raw capture timestamps. |
| `deduplicate` | boolean | no | `false` | PCAPNG only: suppress identical frame bytes observed on different input interfaces within 10 ms. |
| `metadata_policy` | `preserve` or `minimal` | no | `preserve` | Preserve declared PCAPNG metadata or emit neutral interface names without section metadata. |
| `metadata` | object | no | empty | PCAPNG section metadata. |

Use one of `endpoint`, `endpoints` or `segments`. A classic `pcap` capture has exactly one endpoint;
segment and multi-interface captures require `format: pcapng`. Each `endpoints` item needs a distinct
`name`. Segment capture expands to one observation point per attached link in the compiled plan.
Links added to GNS3 after compilation are not included. Empty or unknown segments are rejected at
compilation.
Set `deduplicate: true` when endpoints observe the same Ethernet segment and the export should be a
single logical trace rather than a multi-vantage capture.

An `endpoint` or `endpoints` item contains:

| Field | Type | Required |
| --- | --- | --- |
| `node` | `Identifier` | yes |
| `interface` | `Identifier` | yes |
| `name` | `Identifier` or null | no for `endpoint`; yes for `endpoints` |
| `description` | string or null | no |

`metadata` contains:

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `comment` | string or null | no | PCAPNG section comment. |
| `hardware` | string or null | no | Capture hardware description. |
| `os` | string or null | no | Capture operating-system description. |
| `application` | string or null | no | Application name recorded in the PCAPNG section. |

Supported `data_link_type` values are:

```text
DLT_ATM_RFC1483
DLT_EN10MB
DLT_FRELAY
DLT_C_HDLC
DLT_PPP_SERIAL
```

During compilation, each endpoint resolves to one concrete scenario interface and therefore one
concrete GNS3 link. Two capture declarations cannot resolve to the same underlying link. PCAPNG
exports merge endpoint streams in timestamp order and retain the interface names/descriptions.

## `evidence`

Evidence is staged internally until the realism audit completes. A rejected export remains under
`internal/` for diagnosis and is not published in `evidence/`.

```yaml
evidence:
  realism_audit:
    enabled: true
    baseline: baselines/office-hour.pcapng
    fail_on: error
```

| Field | Type | Required | Default | Meaning |
| --- | --- | --- | --- | --- |
| `realism_audit` | object | no | enabled | Audit policy applied before evidence publication. |
| `enabled` | boolean | no | `true` | Write and evaluate the audit report. |
| `baseline` | relative file path or null | no | `null` | Optional real PCAP/PCAPNG capture used for profile comparison. |
| `fail_on` | `never`, `error`, or `warning` | no | `error` | Finding severity that blocks evidence publication. |

The audit detects empty captures and capture endpoints, virtual NIC OUIs, lab-specific strings in
packet payloads, low source diversity, DNS and HTTP anomalies, long idle gaps, and packet-volume,
protocol, and source-diversity gaps against a baseline. When TLS key logs are staged, it also
checks decrypted traffic for fatal TLS alerts and HTTP/HTTP2 errors using `tshark`; if that check
cannot run, the report records a warning. Accelerated retiming is flagged because guest protocol
timers do not follow the mapped packet timestamps.
Its JSON report is written to `internal/realism-audit.json` before delivery.

## Strictness and validation stages

Validation occurs in layers:

1. **Pydantic schema validation** rejects unknown fields, invalid scalar values and structural errors.
2. **Cross-reference validation** checks segment/profile/group references.
3. **Compilation** expands node groups, allocates IP addresses, resolves selectors/events/captures and rejects ambiguity.
4. **Backend preflight** checks GNS3-specific constraints such as template availability/type and concrete port inventories before deployment.

Use:

```bash
labfoundry validate scenario.yaml
```

to run schema validation and full compilation without contacting GNS3.

## Machine-oriented schema API

For exact Python types, defaults, validators and source code, use the [Schema API reference](api/schema.md). The API reference is generated from the implementation with mkdocstrings, while this page defines the author-facing YAML contract.
