# Architecture

## Core invariant

A scenario is validated and compiled **before** LabFoundry creates infrastructure. The frozen
`ScenarioPlan` is the execution contract for one scenario/seed; GNS3 UUIDs and guest transport
credentials never belong in the authoring DSL.

```text
scenario.yaml
    │
    ▼
Pydantic schema ── deterministic compiler/IPAM/selectors ──► ScenarioPlan
                                                               │
               ┌───────────────────────────────────────────────┼─────────────────────┐
               ▼                                               ▼                     ▼
          GNS3Backend                                    GuestManager           Runtime/Evidence
      topology/NAT/links                          VBox/Incus/SSH exec       clock/events/PCAP
```

The GNS3 Controller API is an infrastructure backend. Application behavior remains inside the
actual guest operating system or container.

## Determinism

Randomness is namespaced with BLAKE2b-derived 64-bit seeds. IPAM, selectors and each recurring
traffic stream receive independent sub-seeds. Adding an unrelated procedural choice should not
reshuffle every other choice in the same scenario.

## GNS3 boundary

LabFoundry 0.2 uses documented Controller API v2 routes and supports GNS3 Server
`>=2.2.61,<2.3.0`. Local PCAP streaming uses the `/v2/compute/` route on the controller origin;
other infrastructure requests use Controller routes.

Guest nodes, segment switches and packet captures run on one GNS3 compute so their capture clocks
share a time base. The built-in NAT node normally runs there too, but `--nat-compute` can place it
on another connected compute.

Each logical segment compiles to a built-in Ethernet switch:

```text
segment users
    │
__lf_seg_users
    ├── ws-01 adapter 0
    ├── ws-02 adapter 0
    └── gateway adapter 0
```

IP addresses belong to the guest/scenario layer, not to GNS3 links.

## Guest execution boundary

LabFoundry exposes general execution primitives rather than protocol/application integrations.
A scenario can therefore run `curl`, Chromium/Playwright, Git, SMTP tools, package managers or
arbitrary lab scripts without a LabFoundry-specific feature.

```text
GuestManager
   ├── VirtualBoxGuestExecutor ── VBoxManage guestcontrol / Guest Additions
   ├── IncusGuestExecutor      ── scenario VM / local Incus socket
   └── SSHGuestExecutor        ── key-based OpenSSH
```

The scenario contains only a `guest_profile` name. Transport endpoints, keys and passwords live
in a separate guest inventory. VirtualBox Guest Additions provide control
independently of the simulated NICs.

The generic runtime actions are:

- `guest.exec` — bounded synchronous argv or shell/PowerShell execution;
- `guest.start` / `guest.stop` / `guest.status` / `guest.wait` — tracked long-running process trees;
- `guest.upload` / `guest.copy` / `guest.download` — sandboxed file transfer;
- `guest.mkdir` — guest directory creation.

`GuestManager` accepts a `GuestExecutorFactory`. Its `validate` method checks transport prerequisites
before deployment; `create` receives the compiled node, profile and deployment state and returns a
`GuestExecutor`. The default factory selects VirtualBox, Incus or SSH. A replacement factory can
reuse the existing inventory profiles without changing action dispatch or scheduling. A new inventory
transport also needs a profile in the discriminated `GuestProfile` union.

Executors may implement `HostClockControl` to disable host-driven clock correction. The manager
checks that capability instead of a concrete executor class. Hostname management, Linux sudo and
clone identity checks follow the profile's Python policies.

Guest provisioning applies compiled static IPv4/DHCP configuration, gateways, DNS resolvers and
optional IPv4 forwarding before evidence capture starts.

## Node lifecycle adapters

`GNS3Backend` accepts a `node_lifecycles` mapping keyed by GNS3 template type. Entries override or
extend the default VirtualBox adapter. A `NodeLifecycle` validates template settings, configures
per-node resources through `prepare_node`, resolves the
guest runtime identifier and handles preparation, post-start verification and post-deletion cleanup.
Positive startup priorities request sequential node starts; lower priorities start first.

VirtualBox adapter counts, clone names and local Windows registration operations live in
`backends/gns3/virtualbox.py`. UDP tunnel diagnostics remain host checks. Node types without an adapter
use the ordinary GNS3 lifecycle. Guest transport and node lifecycle adapters are independent: an SSH
executor can control a VM whose lifecycle is handled by another GNS3 node adapter.

These injection points are Python APIs. Adding an adapter does not establish support for a new
hypervisor until its template, startup, guest control and cleanup paths have been tested.

An `IncusEnvironment` owns a linked scenario VM clone and its address/UDP leases. Managed GNS3
endpoints carry Ethernet frames between the existing graph and unprivileged containers. The
container clock domain is the outer VM; `GuestManager` synchronizes it once while capture and
scheduling keep the runner's reference clock. Snapshots pair the project with the scenario VM.
See [Incus environments](../guide/incus.md) for prerequisites and the shared-kernel boundary.

## Infrastructure actions

The runtime can also manipulate the GNS3 project during a scenario:

- `gns3.node.start`, `gns3.node.stop`, `gns3.node.restart`;
- `gns3.link.update` for suspend state and controller-advertised link filters;
- `gns3.snapshot.create`, `gns3.snapshot.restore`, `gns3.snapshot.delete`.

Filter parameters are checked against `/available_filters` before an update is sent.

Node actions take `parameters.node` as a concrete compiled node name. Link updates take
`parameters.link` as a compiled link name and at least one of `suspend` or `filters`.
Snapshot actions take `parameters.name`; restore/delete resolve exactly one snapshot of that name.

Snapshot creation and restoration stop the project and restart only nodes that were running.
Node start/restart and snapshot restart wait for guest transports and reapply compiled networking.
Shutdown invalidates tracked process handles. These actions do not rerun provision scripts,
restart tracked services, restore action results, or resynchronize guest clocks. Declare any
required service recovery explicitly in subsequent events; a snapshot is not a runtime checkpoint.

## Real-world egress

No public Internet route is created implicitly. `network.egress.mode: nat` creates the built-in
GNS3 NAT node and connects it to one explicitly selected logical segment. Guests on that segment
may then use ordinary DHCP/connectivity and run whatever networking software is installed.

Cloud templates expose host interfaces more directly, so they are denied unless
`network.allow_cloud_nodes: true` is explicitly set. The backend repeats this check after actual
GNS3 template resolution so omitting `template_type` is not a bypass.
NAT templates are rejected in ordinary node groups, including after template resolution. NAT
connections must be declared through `network.egress`.

## Runtime lifecycle

`labfoundry run` performs this lifecycle:

```text
compile/preflight
      ↓
deploy GNS3 project
      ↓
start nodes
      ↓
healthcheck already-bound guest transports
      ↓
configure guest networking
      ↓
optional clock sync + provision + identity checks + optional clock sync
      ↓
start captures
      ↓
ScenarioClock + events + recurring actions
      ↓
stop captures + drain/download raw streams
      ↓
stop tracked guest processes + refresh final downloads
      ↓
export captures + audit + publish accepted evidence
      ↓
stop nodes + optional project deletion + final metadata
```

Deployment and initial metadata are recorded before runtime. Failures trigger best-effort evidence
finalization and cleanup. Run directories are never silently reused when non-empty.

## Time model

Four time sources are distinct:

1. the runner's monotonic clock measures elapsed time and scheduling delays;
2. the scenario clock maps that elapsed time to a timezone-aware story timestamp;
3. guest wall clocks drive application dates, logs and certificate checks;
4. GNS3 compute clocks timestamp raw capture records.

GNS3 captures are produced by compute infrastructure, so changing a VM clock cannot be assumed to
change PCAP record timestamps. Raw captures stay private. During a run, exported PCAP or PCAPNG
records are mapped through runtime clock segments using:

```text
T_export = T_segment_scenario_start + (T_raw - T_segment_raw_start) * segment_speed
```

Without gap warping there is one runtime segment. With `clock.warp_gaps`, each warp starts another
segment: the scenario clock advances over the idle period without scaling packet timing within an
active segment. When no runtime segments are supplied, the exporter falls back to the earliest
observed packet across the capture set as its raw anchor. Neither mode can infer activity before
the first observed frame.

Runtime segments use the runner's epoch clock as their raw anchor. Placing captures on one compute
does not synchronize that compute with a remote runner; keep their wall clocks aligned for retiming.
Capture starts before the runtime clock is anchored, so setup frames may map before `clock.start`.

Without warping, different timestamp batches run concurrently; events within one batch run in
compiled sequence order. Long commands can overlap later batches. `after` is relative to a compiled
timestamp, not the earlier command's completion. A `when` condition waits for the referenced command
result, which can delay the dependent action beyond its scheduled timestamp.

With `warp_gaps`, batches run serially. A delay greater than `settle_delay` triggers a settle wait
and a Linux pending-connection check in the default and named network namespaces before the
scenario clock jumps. The check uses a timeout budget of the lesser of 130 seconds and the remaining
gap; Windows is not inspected.
It does not wait for all established connections or arbitrary application work to finish.

Recurring streams wait a seeded interval before their first invocation and after each invocation
finishes. Their sampled delays are reproducible, but wall-clock start times also depend on command
duration. `clock.duration` stops new recurring invocations at the scenario deadline; actions already
running are awaited. Without a duration, runtime ends when all scheduled actions finish. There is
no extra packet-drain delay after the final action.

## Failure handling and reruns

When runtime fails after capture startup, the runner still attempts capture finalization, final
downloads and auditing. If those succeed, captures can be published even though the run fails.
Read `manifest.json`'s `extra.status` and the private journal before treating evidence as complete.
A failure before deployment finishes may leave only a journal, without a final manifest.
Finalization and cleanup failures are attached to the primary error when possible.

`--keep-project` retains the project on success or failure; `--keep-on-failure` retains it only on
failure under the default destruction policy. Both still stop tracked guest processes and nodes.
Retaining a project does not keep the exercise running.
Retention flags do not prevent rollback of a deployment that fails before it returns deployment state.

There is no interrupted-run resumption. `run.jsonl` is diagnostic history, not an executable
checkpoint, and saved plans or deployment metadata cannot be passed back to `labfoundry run`.
After resolving a failure, rerun the YAML; the CLI compiles it again and reserves a new directory
with an incremented attempt suffix. Snapshot restore and command retries operate within a live
run and do not resume a terminated one.

## Evidence boundary

```text
GNS3 capture
    ↓
internal/raw/*.pcap
    ↓
validation + timestamp preservation or deterministic retiming
    ↓
internal/staged-evidence/ + realism audit
    ↓
evidence/*.pcap or *.pcapng
```

Private ground truth, deployment metadata, raw captures and the audit report remain under
`internal/`. Published downloads, such as TLS key logs, move from staged evidence to `evidence/`
only after the audit accepts the capture.

## Source ownership

`schema/` owns scenario validation; `compiler/` resolves IPAM, selectors, event times, captures and
SVG views. `backends/gns3/` owns controller calls and deployment state. `guest/` owns transport
configuration, image bootstrap, networking, processes and the uploadable timing helpers.
`runtime/` owns the lifecycle, action dispatchers, scheduler, clock and journal; `traffic/` samples
recurring intervals. `evidence/` owns capture streams, timestamp/TCP rewriting and auditing;
`groundtruth/` writes private metadata and artifact hashes. `cli/` exposes operator commands.

Scenario-specific services, client scripts and assets are supplied by the operator. Portable tests
create their own scenario data in `tests/`; real-host tests are in `tests/integration/`, and CI/release
workflows in `.github/workflows/`. Guides and references live
under `docs/`; API pages collect the source docstrings through `mkdocs.yml`.
