# Incus scenario environments

An Incus environment runs one Linux VirtualBox VM per scenario and one unprivileged system
container per Incus node. Ordinary VirtualBox nodes can remain in the same scenario when they
need their own kernel or Windows.

Containers run their actual applications and Linux network stacks. Each has its own process,
mount, user, UTS and network namespaces. Containers in an environment share a kernel and calendar
clock. Kernel exploits, kernel modules, boot behaviour and conventional whole-machine disk evidence
require individual VMs. This mode does not provide VM isolation between containers.

## Base VM

Prepare a Linux VirtualBox VM with Guest Additions, Python 3, Incus 6.0 or newer, uBridge,
iproute2, systemd and non-interactive sudo for its control account. Incus must already be
initialized and its storage
pool and container images must be present. The base must contain no Incus instances. Shut it down
and take the snapshot selected by the environment configuration.
Check cold boots with the chosen vCPU count and paravirtualization provider on the target host.

Use clean system-container images with systemd, unique identities on first boot, a POSIX
shell, `setsid`, `runuser` for non-root command users, and the networking and application tools used
by the scenario. Guest profiles select existing accounts inside those images. Non-root accounts
need non-interactive sudo for network provisioning and privileged file installation. Browsers must
retain their sandbox; enable nesting only for images whose namespace requirements need it.

Provide enough subordinate UID/GID ranges for all isolated containers. LabFoundry always selects
unprivileged containers, separate UID/GID maps and no inherited Incus profiles. It does not expose
the Incus management socket or host directories to nodes. Unsafe privilege or raw ID-map settings
fail startup.

The immutable base snapshot stays available while linked clones exist. Install application
versions and libraries in the images before freezing the base. No packages or images are fetched
during scenario startup. Resolved image fingerprints, the kernel and Incus version are recorded
in run metadata.

## Environment configuration

Guest inventories accept an `environments` mapping. Each named entry has these fields:

| Field | Required | Meaning |
| --- | --- | --- |
| `vmname` | yes | Registered VirtualBox base VM. |
| `snapshot` | yes | Immutable base snapshot name or UUID. |
| `guest_profile` | yes | Linux VirtualBox profile used to control the VM through Guest Additions. |
| `host_only_adapter` | yes | Existing VirtualBox host-only adapter name. |
| `host_address` | yes | Host IPv4 address on that adapter. |
| `guest_addresses` | yes | Reserved IPv4 interfaces on the same private network. One address is leased per scenario. |
| `management_interface` | no | VM interface used for tunnels; defaults to `enp0s3`. |
| `memory_mib` | yes | RAM allocated to the entire scenario VM; minimum 512 MiB. |
| `cpus` | yes | VM vCPU count, at most 32 and no more than the host's logical CPU count. |
| `paravirt_provider` | no | Optional VirtualBox clock/paravirtualization provider; otherwise preserve the base setting. |
| `storage_pool` | yes | Existing Incus storage pool for container roots. |
| `startup_concurrency` | no | Container setup/start workers; defaults to 4, maximum 16. Instance creation is serialized to protect UID/GID allocation. |
| `readiness_timeout` | no | Guest Additions boot deadline in seconds; defaults to 180. |
| `shutdown_timeout` | no | Clean VM shutdown deadline; defaults to 60 seconds. |
| `reserve_memory_mib` | no | Free host RAM required in addition to the VM allocation; defaults to 1024 MiB. |

The host-only adapter and address are checked before deployment. Reserve guest addresses outside
DHCP pools and other VM assignments. Their number bounds simultaneous running or retained
scenarios. Address and UDP-port leases live under the runner's `~/.labfoundry/incus/leases/`.
They are released only after project and VM cleanup. A retained project keeps its leases.

The runner clones the snapshot, sets the requested RAM and CPUs, disables the clone's other NICs
and host-driven clock correction, and starts it headlessly. The VM's management network has no
default route and is separate from all node interfaces. The base VM and host network configuration
are not changed. Use a dedicated host-only network; tunnel traffic is not authenticated or encrypted.

## Node configuration

Declare Incus nodes with `template_type` set to `incus`. Their `template` names a local Incus image
alias or fingerprint in the base VM, and `guest_profile` names an Incus profile. All Incus nodes in
one scenario must select the same environment. Interface adapters must be unique and use port 0.

An Incus guest profile uses `transport` set to `incus`, `os` set to `linux`, and an `environment`
name. It accepts the common shell, interface mapping, DHCP and network-control fields.

| Field | Default | Meaning |
| --- | --- | --- |
| `username` | `root` | Existing command user in the container. |
| `memory_mib` | unset | Optional per-container memory limit. |
| `cpus` | unset | Optional CPU count exposed to the container. |
| `processes` | unset | Optional process-count limit. |
| `nesting` | `false` | Permit nested namespaces required by the selected image. |
| `command_timeout` | `300` | Default command deadline in seconds. Explicit action deadlines override it. |

Container resource limits are absent unless configured. LabFoundry does not shrink application
budgets or impose CPU quotas to obtain a lower footprint. Size the outer VM for measured concurrent
application peaks, caches and system overhead. Free host RAM is checked at admission; other
programs can still consume it after that check.

## Topology, clocks and captures

This mode requires a local GNS3 controller and local compute on the VirtualBox host. Each container
NIC reaches its normal GNS3 segment through a dedicated Linux bridge, TAP and UDP Ethernet tunnel.
One VM-local uBridge process handles all these tunnels. Sparse adapter numbers are translated to
the endpoint's GNS3 ports. Unconnected nodes use an unconnected switch placeholder, never an
automatically populated cloud. Managed endpoints contain only explicitly assigned UDP ports;
ordinary cloud-node restrictions remain in force.

Addresses, MACs, routes, DNS and IPv4 forwarding still use normal guest provisioning. Link filters,
link suspension, captures and explicit NAT egress remain in GNS3. The management NIC is never a
node NIC. Allow the assigned UDP traffic through the host firewall. Large Ethernet frames can
fragment on the host-only underlay; measure loss and throughput with its configured MTU.
An unavailable host-only adapter blocks deployment. The controller's NAT support is also
required when the scenario declares NAT egress.

The runner and GNS3 retain their real clocks. Historical time and gap warps change the scenario
VM's clock once for all containers; individual VMs are synchronized separately. Incus control uses
the VM's local Unix socket, so it does not require TLS management certificates valid at historical
dates. Disable independent clock agents in the base. Packet timestamps are collected outside the
warped VM and pass through the existing timestamp mapping and evidence audit.
The runner retains the environment's clock offset across VM restarts and disk restores.

Node start, stop and restart actions also control their containers. Project snapshots quiesce the
environment and pair the GNS3 snapshot with a disk snapshot of the scenario VM. Restore failures
leave a partially restored environment stopped. These are disk snapshots, not live process or
kernel checkpoints. Cleanup removes only the owned VM clone. `internal/deployment.json` records
the resources needed to identify a retained deployment.
Captures are drained into validated fragments during lifecycle operations and resumed before
containers restart. Export joins those fragments without dropping or sorting packets; incomplete
records or a changed capture format fail export.

Sharing a kernel removes duplicated guest kernels. Storage behaviour depends on the selected
Incus pool: copy-on-write disk sharing does not prove page-cache sharing. Measure host-wide RAM,
CPU, pressure, action durations and capture loss with the real workload before claiming a saving.
For VM sizing, compare allocated RAM, Linux `MemAvailable` and the VirtualBox process working
set on the host. These measure different things, and reclaimable guest cache can still occupy
host RAM. Record peak CPU over a stated sampling window and check for saturation. Rerun the
scenario at the proposed allocation, including provisioning and capture export, and check swap,
out-of-memory events and action deadlines.
