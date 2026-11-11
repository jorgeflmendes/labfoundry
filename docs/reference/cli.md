# CLI reference

The `labfoundry` command is the operator interface for validation, compilation and GNS3 operations. The Python callbacks that implement these commands are also available in the [CLI implementation API](api/cli.md).

The CLI is intentionally thin: scenario semantics belong in the schema/compiler/runtime packages rather than command callbacks.

## Global help

```bash
labfoundry --help
labfoundry gns3 --help
labfoundry guest --help
```

## `labfoundry guest configure`

Create a local VirtualBox guest inventory from an operator-supplied inventory template. The command
prompts for the guest username and password, writes the password to a separate file beside the
inventory, and replaces each VirtualBox profile's `password_env` with `password_file`.

```bash
labfoundry guest configure INVENTORY_TEMPLATE --output ~/.labfoundry/guest_inventory.yaml
```

It refuses to overwrite either file. The password is never put in the scenario, the inventory, or
the command line. `--username` skips the username prompt; `--password-file` chooses where the
separate password file is written. Without `--output`, the inventory is written to
`~/.labfoundry/guest_inventory.yaml`.
The username prompt defaults to `rangeforge`; enter the account already configured in your images.

## `labfoundry guest bootstrap-linux`

Create a local first-boot bundle for a cloud-init-enabled Linux VirtualBox base image. It generates
a random guest-control password, a matching inventory, and a VFAT NoCloud disk (`seed/seed.vmdk`).
Attach the disk to the powered-off base image for one boot, then detach it before taking the GNS3
template snapshot.

```bash
labfoundry guest bootstrap-linux INVENTORY_TEMPLATE --output ~/.labfoundry/bootstrap/linux
```

`--username` and `--hostname` set the control account and first-boot hostname; both default to
`labfoundry`. The output defaults to `~/.labfoundry/bootstrap/linux` and must not already exist.
Only Linux VirtualBox profiles are updated; other profiles retain their template settings.

## `labfoundry guest bootstrap`

Create Linux cloud-init or Windows unattended-install assets from a strict bootstrap YAML:

```bash
labfoundry guest bootstrap BOOTSTRAP_YAML INVENTORY_TEMPLATE [--output PATH]
```

The specification declares the hostname, users, password policy (`generated` or `locked`), groups,
administrator and runtime-control roles, packages, services, first-boot commands, and Guest Additions.
Exactly one user must set `control: true`. Passwords are generated locally and written as separate
files under the output directory; the inventory references only the control user's password file.

Linux uses a NoCloud VFAT/VMDK seed. Windows emits `Autounattend.xml` and `bootstrap.ps1`, with
package installation through `winget`.

Without `--output`, the bundle is written to `~/.labfoundry/bootstrap/<specification-stem>`.
The directory must not exist. Only VirtualBox profiles matching the specification's OS are updated.
See the [bootstrap reference](guest-bootstrap.md) for fields and image requirements.

## `labfoundry guest bootstrap-windows`

Create `Autounattend.xml`, a Guest Additions bootstrap script, and a matching local inventory for
a new Windows installation. It is not a password-reset tool for an existing installation.

```bash
labfoundry guest bootstrap-windows INVENTORY_TEMPLATE [--output PATH] [--username TEXT]
```

The output defaults to `~/.labfoundry/bootstrap/windows` and the username to `labfoundry`.
Only Windows VirtualBox profiles are updated. The output directory must not already exist.

## `labfoundry validate`

Validate the YAML and fully compile it without contacting GNS3.

```bash
labfoundry validate SCENARIO [--seed INTEGER]
```

| Argument / option | Meaning |
| --- | --- |
| `SCENARIO` | Existing readable scenario YAML file. |
| `--seed INTEGER` | Override `scenario.seed` for this compilation. |

The command exits with status `2` when LabFoundry validation/compilation fails.

## `labfoundry compile`

Compile the scenario into the frozen JSON execution plan.

```bash
labfoundry compile SCENARIO [--output PATH] [--seed INTEGER]
```

| Argument / option | Meaning |
| --- | --- |
| `SCENARIO` | Existing readable scenario YAML file. |
| `--output`, `-o` | Destination JSON path. Defaults to the scenario path with `.plan.json`. |
| `--seed INTEGER` | Override `scenario.seed`. |

## `labfoundry inspect`

Render resolved nodes/interfaces and the compiled event timeline.

```bash
labfoundry inspect SCENARIO [--seed INTEGER]
```

This is useful for instructors because it reveals deterministic procedural choices before infrastructure deployment.

## `labfoundry diagram`

Draw the compiled network topology as an SVG without starting GNS3. Nodes use
type-specific icons; segment switches and capture links are shown too. The SVG uses its
own aligned layout: multi-segment nodes above segment bands, switches beside device rows,
and orthogonal links routed outside the device cards. Names wrap and interface addresses
are printed below the icons. This does not change the compiled GNS3 deployment positions.

```bash
labfoundry diagram SCENARIO [--output PATH] [--seed INTEGER]
```

The default output is the scenario path with an `.svg` extension.

```bash
labfoundry diagram SCENARIO --view captured -o students.svg
labfoundry diagram SCENARIO --capture users-perimeter --segment users -o users.svg
labfoundry diagram SCENARIO --exclude-segment servers --exclude-node dns -o selected.svg
```

`--view captured` keeps only links declared in the scenario's capture definitions and
their devices, segments and interfaces. Uncaptured gateway addresses and the deployment
NAT are omitted, including their tooltips. This describes configured capture coverage;
it does not inspect a PCAP or claim that every listed address generated packets.
Templates and the compiler seed are also omitted from this view.

`--capture NAME` selects one or more named captures and implies the captured view.
`--segment`, `--exclude-segment`, `--node` and `--exclude-node` filter the topology;
each option can be repeated, exclusions win, and unknown names cause an error.
Node filters use concrete compiled node names or the NAT name. Filtering removes the
affected interfaces and links as well as the boxes. The layout is recalculated for
the remaining devices. Captured views default to `.captured.svg` so they do not replace
the full diagram.

## `labfoundry flow`

Draw scheduled actions as a chronological SVG in a compact column, with source and destination
shown beside each action. `--layout lanes` uses a lane for each participating node instead.
This compiles the scenario without contacting GNS3 or executing guest commands.

```bash
labfoundry flow SCENARIO --mode narrative -o narrative.svg
labfoundry flow SCENARIO --mode complete -o complete.svg
labfoundry flow SCENARIO --mode narrative --day 2025-04-17 -o daily.svg
```

Narrative mode reads `# Narrative:` comments immediately before each event, including continuation
comment lines. The parser also accepts the legacy `Narrativa` marker. Without a narrative comment,
the event ID is displayed with underscores replaced by spaces. No command is interpreted to invent
a description or destination.

Complete mode includes the full compiled action parameters, result conditions,
preparation actions, generated background events, recurring traffic definitions,
clock settings, compiled topology/addressing and capture definitions. Recurring traffic is shown separately because
its execution times depend on the runtime. Source and destination arrows use the
declared node references; they do not claim that an action succeeded or caused the next one.

`--background` / `--no-background` and `--provision` / `--no-provision` override the
mode defaults: both are off in narrative mode and on in complete mode. `--day` filters
scheduled events by their timestamp's calendar date; preparation and recurring
definitions remain included when enabled. Dates form section headers, and simultaneous
events retain compiled sequence order. Vertical spacing follows text length rather
than elapsed time. `--seed` overrides the scenario seed.

The default output is `SCENARIO.flow.narrative.svg` or `SCENARIO.flow.complete.svg`.
SVG text remains selectable; long parameters wrap without dropping their content.

## `labfoundry run`

Execute a compiled scenario end-to-end: deploy the GNS3 topology, bind guest transports, configure guest networking, start captures, run generic guest/infrastructure actions, export PCAP evidence, stop nodes and clean up the project.

```bash
labfoundry guest configure INVENTORY_TEMPLATE --output ~/.labfoundry/guest_inventory.yaml
labfoundry run SCENARIO --guest-config ~/.labfoundry/guest_inventory.yaml
```

| Argument / option | Default | Meaning |
| --- | --- | --- |
| `SCENARIO` | — | Existing scenario YAML file. |
| `--scenario`, `-s` | unset | Alternative to the positional `SCENARIO` path. |
| `--guest-config PATH` | `~/.labfoundry/guest_inventory.yaml` if it exists | Separate guest transport/credential inventory. |
| `--asset-dir PATH` | unset | Directory for `@assets/` upload sources. |
| `--run-root`, `--out` | `runs` | Root for evidence and private metadata. |
| `--seed INTEGER` | scenario seed | Override the deterministic root seed. |
| `--keep-project/--destroy-project` | destroy | Retain the GNS3 project on success or failure. Nodes are still stopped. |
| `--keep-on-failure/--cleanup-on-failure` | cleanup | Preserve the failed project for debugging. |
| shared GNS3 options | — | See [GNS3 connection options](#gns3-connection-options). |

Guest passwords are not stored in the scenario plan. VirtualBox inventories created by
`labfoundry guest configure` reference a separate password file.

If both scenario forms are supplied, `--scenario` takes precedence. Relative run and output paths
are resolved from the working directory; upload paths are resolved from the scenario directory
unless they use `@assets/` or `@helpers/`.

There is no CLI option to resume a journal, execute a saved JSON plan, or attach a run to an
existing deployment. Every invocation compiles the YAML and reserves a new run directory.
See [failure handling and reruns](../advanced/architecture.md#failure-handling-and-reruns).


## GNS3 connection options

GNS3 commands share these settings where applicable:

| Option | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `--url TEXT` | `LABFOUNDRY_GNS3_URL` | `http://127.0.0.1:3080` | Controller base URL. |
| `--username TEXT` | `LABFOUNDRY_GNS3_USER` | unset | HTTP Basic username. |
| `--password TEXT` | `LABFOUNDRY_GNS3_PASSWORD` | unset | HTTP Basic password. Prefer the environment to command-line arguments. |
| `--compute TEXT` | `LABFOUNDRY_GNS3_COMPUTE` | `local` | GNS3 compute ID. |
| `--nat-compute TEXT` | `LABFOUNDRY_GNS3_NAT_COMPUTE` | selected compute | Compute used for the built-in NAT node during `run`. |
| `--verify-tls/--no-verify-tls` | — | verify | TLS certificate verification. |

Credentials should be provided through environment variables or another process-level secret mechanism rather than committed scenario files.

## `labfoundry gns3 check`

Check Controller compatibility, selected compute status/capabilities and the number of available VirtualBox templates.

```bash
labfoundry gns3 check [OPTIONS]
```

The current implementation requires GNS3 Server `>=2.2.61,<2.3.0` and is validated against the 2.2.61 Controller API contract.

## `labfoundry gns3 create-vbox-template`

Create a GNS3 VirtualBox template that references a VM already registered in VirtualBox on the selected compute.

```bash
labfoundry gns3 create-vbox-template \
  --name "RF Ubuntu Client VBox" \
  --vmname "RF-Ubuntu-Base" \
  --linked-clone \
  --headless \
  --adapters 2 \
  --ram 256
```

| Option | Default | Constraint / meaning |
| --- | --- | --- |
| `--name TEXT` | prompted | GNS3 template name. |
| `--vmname TEXT` | prompted | Exact VirtualBox VM name. |
| `--linked-clone/--no-linked-clone` | enabled | Whether GNS3 should create linked clones. |
| `--headless/--no-headless` | enabled | Run VM headlessly. |
| `--adapters INTEGER` | `2` | `0..36`. |
| `--ram INTEGER` | `256` | `0..65535` MB according to the mirrored GNS3 schema. |
| shared GNS3 options | — | See [GNS3 connection options](#gns3-connection-options). |

## `labfoundry gns3 deploy`

Compile and deploy the topology to GNS3.

```bash
labfoundry gns3 deploy SCENARIO [OPTIONS]
```

| Argument / option | Default | Meaning |
| --- | --- | --- |
| `SCENARIO` | — | Existing scenario YAML file. |
| `--run-root PATH` | `runs` | Root directory for private run metadata. |
| `--seed INTEGER` | scenario seed | Override the DSL seed. |
| `--start/--no-start` | `--no-start` | Start deployed nodes after creation. |
| shared GNS3 options | — | See [GNS3 connection options](#gns3-connection-options). |

Deployment writes the internal plan, ground-truth/reproducibility metadata and deployment state below:

```text
<run-root>/<scenario-id>/<seed>/internal/
```

Repeated deployments with the same scenario and seed use `<seed>-2`, `<seed>-3`, and so on
instead of overwriting an earlier run.

This low-level command deploys infrastructure only. Use `labfoundry run` when guest provisioning, actions, captures and cleanup should be orchestrated automatically.

Unlike `run`, `deploy` leaves the project in GNS3 on success and does not accept `--nat-compute`.
It rolls back failed deployment or metadata finalization. It does not generate runtime captures.

## Exit behavior

LabFoundry commands convert expected framework/configuration/backend failures into a user-facing error and exit status `2`. Unexpected Python defects are not intentionally swallowed by the core libraries and should be treated as bugs.

## Authoritative runtime help

The installed CLI remains the authoritative source for the exact Typer rendering of command options:

```bash
labfoundry --help
labfoundry validate --help
labfoundry compile --help
labfoundry inspect --help
labfoundry diagram --help
labfoundry flow --help
labfoundry run --help
labfoundry guest configure --help
labfoundry guest bootstrap --help
labfoundry guest bootstrap-linux --help
labfoundry guest bootstrap-windows --help
labfoundry gns3 check --help
labfoundry gns3 create-vbox-template --help
labfoundry gns3 deploy --help
```
