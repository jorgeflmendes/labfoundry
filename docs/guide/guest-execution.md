# General guest execution

LabFoundry does **not** implement application-specific operations such as `email.send`, `youtube.search`, or `git.clone`. It provides general out-of-band primitives so the guest operating system can run the real tool that performs the action.

## Guest profiles

Guest startup retries transport-not-ready errors. An explicit VirtualBox login rejection stops
the run immediately with an inventory error instead of waiting for the boot timeout.
Readiness checks run concurrently, with a default deadline of 600 seconds per guest and a
three-second polling interval. These values are Python API settings, not CLI options.

Scenario files contain only a profile reference:

```yaml
network:
  nodes:
    ws:
      template: "RF Ubuntu Client VBox"
      template_type: virtualbox
      guest_profile: linux-vbox
      interfaces:
        eth0:
          segment: users
          ip: auto
          adapter: 0
```

Secrets and transport details live in a separate inventory. Unknown fields and invalid profile
names are rejected.

```yaml
profiles:
  linux-vbox:
    transport: virtualbox
    os: linux
    username: labfoundry
    password_env: LABFOUNDRY_LINUX_PASSWORD
    shell: /bin/sh
    interface_map:
      eth0: enp0s3
    dhcp_command: "dhclient -1 -H {hostname} {interface}"
```

Supported transports are:

- `virtualbox`: `VBoxManage guestcontrol`; requires VirtualBox Guest Additions and uses a password environment variable or password file.
- `ssh`: system OpenSSH client with key-based authentication; intended for an isolated management interface or an external guest.
- `incus`: local Incus control through a dedicated scenario VM's Guest Additions. See [Incus environments](incus.md) for VM resources, images and isolated tunnel configuration.

VirtualBox commands run on the runner host, which must see the same registered runtime VMs as the
selected compute. A remote controller URL does not forward `VBoxManage` calls to another host.

Every controlled interface needs an `interface_map` entry. For a multi-interface node, map each
logical interface to its own guest device. Separate profiles can share credentials while using
different mappings. Relative password and SSH key/known-hosts paths use the inventory's directory.
SSH verifies host keys by default; configure the management host and key files before the run.

Linux profiles default to `network_control: coexist`, which leaves the guest's network managers
running. Set it to `exclusive` only when LabFoundry should stop NetworkManager and
systemd-networkd, stop an existing DHCP client on the mapped interface and take over its
addressing. Keep a separate management path if the guest needs one.

## Run a command

Guest scripts can upload the bundled timing helpers without installing LabFoundry inside the VM:
`guest.upload` accepts `source: "@helpers/browser_timing.py"` or
`source: "@helpers/response_timing.py"`. Place the helper beside the script that imports it.
Only these named helpers are available through this prefix; other source-path restrictions remain.

Use `guest.exec` with either `argv` or `script`:

```yaml
- at: "09:10"
  action: guest.exec
  from: ws
  parameters:
    argv: ["git", "clone", "https://github.com/example/project.git"]
    timeout: 2m
```

`env` adds command environment variables and `cwd` sets its working directory. `check` defaults
to `true`, so a non-zero exit raises an execution error. Timeline and provision commands have no
default timeout; recurring `guest.exec` uses 60 seconds unless a timeout is supplied. Timeouts,
retry delays and process polling use real elapsed seconds regardless of `clock.speed`.
Invalid command timeouts, retry delays and process polling intervals are rejected during preflight.

Shell/PowerShell scripts are useful when the command should use LabFoundry context:

```yaml
- at: "09:12"
  action: guest.exec
  from: ws
  to: server
  parameters:
    script: 'curl -fsS "http://$LABFOUNDRY_TARGET_IP/" >/dev/null'
    timeout: 30s
```

### Retry transient commands

`guest.exec` can retry transport/application failures without adding service-specific logic to the
framework:

```yaml
- at: "09:10"
  action: guest.exec
  from: ws
  parameters:
    argv: ["curl", "-fsS", "https://example.com/"]
    retry:
      attempts: 4
      delay: 2s
      backoff: 2.0
```

Retries only happen when the guest command raises an execution error. `check: false` deliberately
turns a non-zero guest exit into a normal result, so it does not trigger retry.

LabFoundry injects these protected context variables into commands:

| Variable | Meaning |
| --- | --- |
| `LABFOUNDRY_SEED` | deterministic root seed |
| `LABFOUNDRY_SCENARIO_ID` | scenario ID |
| `LABFOUNDRY_SCENARIO_START` | configured scenario start |
| `LABFOUNDRY_NODE` | executing node |
| `LABFOUNDRY_NODE_IP` | static IP on a segment shared with the target, otherwise the first static IP |
| `LABFOUNDRY_TARGET` | compiled `to` node, when present |
| `LABFOUNDRY_TARGET_IP` | target's static IP on a segment shared with the source, otherwise its first static IP |

IP variables are omitted when no static address is available; DHCP addresses are not discovered
for this context. User-supplied `env` cannot override context variables that LabFoundry populates.

## Long-running processes

```yaml
- at: "09:00"
  action: guest.start
  from: ws
  parameters:
    id: office-activity
    script: python3 /opt/scenario/office_worker.py
    ready:
      script: test -f /tmp/office-worker-ready
      timeout: 30s
      interval: 500ms

- at: "16:55"
  action: guest.stop
  from: ws
  parameters:
    id: office-activity
```

Processes started by LabFoundry are also cleaned up when the scenario ends or fails. `ready` is optional; when present, the start action waits for its guest-side probe to exit successfully and fails if the process exits or the timeout expires. A listener can use a probe such as `ss -ltn | grep -q ':3128 '`.

Tracked processes can be asserted or awaited without application-specific helpers:

```yaml
- at: "09:05"
  action: guest.status
  from: ws
  parameters:
    id: office-activity
    running: true

- at: "16:50"
  action: guest.wait
  from: ws
  parameters:
    id: office-activity
    timeout: 2m
    interval: 500ms
```

`guest.wait` returns when the tracked process exits and forgets its runtime handle. A timeout fails
the scenario rather than silently continuing with unknown process state.
It does not collect the detached process's exit status or output. Use a guest-side receipt or
an explicit `guest.exec` assertion when successful completion matters.

## Provision before capture

Use top-level `provision` when setup should happen after guest networking is configured but before
student-visible packet capture starts:

```yaml
provision:
  - nodes: ws
    action: guest.upload
    parameters:
      source: scripts/office_worker.py
      destination: /opt/scenario/office_worker.py

  - nodes: ws
    action: guest.exec
    parameters:
      script: chmod 0755 /opt/scenario/office_worker.py
```

Provision actions support `guest.exec`, `guest.start`, `guest.upload`, `guest.copy`, and `guest.mkdir`,
and are expanded across every concrete node in the selected group. `guest.start` can use the same
`ready` probe as a timeline event. Provisioning finishes before captures start, keeping its
installation and configuration traffic out of the evidence PCAPs. Long-running services started
there can continue into the capture window.

## Upload/download

Before capture starts, Linux VirtualBox guests are checked for duplicate machine IDs and SSH host
public keys. A duplicate stops the run. The check does not rotate keys or change guest identities;
cloned images need distinct identities provisioned before this gate.

`guest.upload` requires a regular file whose resolved `source` remains below the scenario directory:

```yaml
- at: "08:59"
  action: guest.upload
  from: ws
  parameters:
    source: scripts/office_worker.py
    destination: /opt/scenario/office_worker.py
```

For large, separately prepared assets, use `source: "@assets/name.zip"` and pass
`--asset-dir PATH` to `labfoundry run`. The path remains confined to that directory.

`guest.copy` takes the same `source`/`destination` fields, with a source guest in `from` and a
destination guest in `to`. It downloads to a temporary host file and uploads through the destination
transport; it does not generate a guest-to-guest data-plane transfer.

By default, `guest.download` writes below `internal/downloads` in the run directory:

```yaml
- at: "16:58"
  action: guest.download
  from: ws
  parameters:
    source: /tmp/activity.log
    destination: ws/activity.log
```

Set `publish: true` to stage a file for publication beside the PCAP after the realism audit accepts
the run. The destination remains relative to the run directory's staged evidence area. This is
useful for TLS key logs that should be delivered with the capture:

```yaml
- at: "16:59"
  action: guest.download
  from: ws
  parameters:
    source: /tmp/browser.keys
    destination: browser.keys
    publish: true
    refresh_at_end: true
```

`refresh_at_end` repeats the download after captures and tracked guest processes stop, before
the evidence audit. Use it for growing key logs. Leave it disabled for point-in-time snapshots.
On a runtime failure, unconditional downloads with this flag are attempted even if their timeline
event was not reached. Missing files are recorded in the run journal without stopping other
downloads. Conditional downloads are refreshed only after their event has started.
Final refreshes require capture finalization to reach its post-capture hook. Runs without captures
do not execute that hook, so do not rely on `refresh_at_end` there.

## Recurring activity

Background profiles can use every generic guest primitive. `guest.exec` receives a bounded default
timeout; use it for repeated application activity. Process, transfer, and filesystem actions retain
the same validation as timeline events, so recurring `guest.start`/`guest.stop` actions should use
stable process IDs and intentional intervals.

```yaml
traffic:
  profiles:
    office:
      actions:
        - action: guest.exec
          to: web
          interval:
            distribution: exponential
            mean: 2m
          parameters:
            script: 'curl -fsS "http://$LABFOUNDRY_TARGET_IP/" >/dev/null || true'
  assignments:
    - profile: office
      nodes: ws
```

`clock.duration` is mandatory when recurring traffic is configured so the lifecycle has a deterministic end.

## Linux DHCP command

The default Linux DHCP command is `dhclient -1 {interface}`. To send the compiled hostname,
explicitly set `dhcp_command: "dhclient -1 -H {hostname} {interface}"` as in the inventory above.
Images with a different DHCP client can override `dhcp_command` in the guest profile, for example:

```yaml
dhcp_command: "udhcpc -i {interface} -q"
```

This setting is a transport/provisioning detail, so it belongs in the guest inventory rather than
the scenario DSL.

## Guest clocks and timing helpers

`clock.sync_guests: true` sets configured guest wall clocks after networking, again after provision
and identity checks, and after each warped gap. VirtualBox host-time correction is disabled before
startup. Linux uses privileged `date` and attempts `timedatectl set-ntp false`; Windows uses
PowerShell `Set-Date`. Prepare permissions and disable competing time services in the image.
The runtime does not continuously synchronize clocks, change their rate, or restore time-service
settings on retained guests. Node restarts and snapshot restores do not repeat the clock sync.

The uploadable `browser_timing.py` helper waits for document, font and visible-image readiness and
a stable resource count. Its defaults are a 15-second load deadline and 0.8 seconds of quiet;
resource failures raise unless the caller supplies exact expected URL/status pairs. Its
`reading_delay` samples a bounded pause from the caller's random state. It does not launch a browser.

`response_timing.py` creates seeded first-byte and cumulative-byte schedules, validates complete
body coverage, maps offsets when response bytes are edited, and reports bytes due at an elapsed
time. The service consuming a schedule must perform the actual waits and writes. Neither helper
changes TCP framing or network latency.

## Troubleshooting

- A rejected VirtualBox login is an inventory/image mismatch; use the credentials for that base
  image. A guest that never becomes ready needs working Guest Additions and a reachable transport.
- A missing interface mapping or wrong device name prevents networking setup. Confirm mappings
  against the image; LabFoundry does not infer them from GNS3 adapter numbers.
- Linux permission failures usually require passwordless sudo for the control account. Windows
  network and clock operations need an administrative account.
- Duplicate machine IDs or host keys require preparing distinct clone identities before capture;
  the runtime only checks them.
- A pending-connection failure before warping means Linux SYN-SENT sockets remain. Resolve the
  unreachable destination or have the scenario close its client before the next gap.

Use `internal/run.jsonl` to locate the failed lifecycle phase or action. Retain a failed project
with `--keep-on-failure` when guest inspection is needed, then rerun after fixing the cause.
