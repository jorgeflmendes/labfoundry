# Quickstart

Create a scenario YAML using the [Scenario DSL](../reference/scenario-dsl.md). It must declare
`scenario.id`, `scenario.seed`, a timezone-aware `clock.start`, and the `network.segments` and
`network.nodes` mappings. Add events, recurring traffic and capture endpoints as needed.

The commands below use `SCENARIO` for the path to your scenario, `INVENTORY_TEMPLATE` for your guest
inventory template, and `GUEST_INVENTORY` for the configured inventory. Supply your own files.

Validate and inspect the resolved plan without contacting GNS3:

```bash
labfoundry validate SCENARIO
labfoundry inspect SCENARIO
labfoundry compile SCENARIO -o plan.json
```

## Prepare the guests

Register the templates named in your scenario on the selected GNS3 compute. Prepare each image
with the applications and services its commands need. Set each controlled node's `guest_profile`
to an entry in the [guest inventory](../guide/guest-execution.md). In each profile, map the
scenario's logical interfaces to guest devices with `interface_map`. Use separate profiles when
credentials or mappings differ.

### VirtualBox

1. Register the base VM in VirtualBox on the GNS3 compute and install Guest Additions.
   Check that `VBoxManage --version` works on the runner and that `VBoxManage list vms` shows
   the base VM. Set the inventory's `vboxmanage` field to the executable path if it is outside
   `PATH`. Linux clones need distinct machine IDs and SSH host keys.
2. Register a GNS3 VirtualBox template for that base VM with enough adapters for the scenario.
   Set the node's `template_type` and the profile's `transport` to `virtualbox`. In the
   inventory template, set the guest OS, username and interface mappings. The guest account
   needs the permissions described in the [installation guide](installation.md#runtime-dependencies).
3. Configure the guest credentials interactively:

   ```bash
   labfoundry guest configure INVENTORY_TEMPLATE --output GUEST_INVENTORY
   ```

See the [VirtualBox guide](../guide/virtualbox.md) for linked clones, Guest Additions and
first-boot preparation. To run system containers in a shared VirtualBox VM, follow the
[Incus guide](../guide/incus.md).

## Connect and run

Start GNS3 Server. The default controller is `http://127.0.0.1:3080` and the default compute ID
is `local`. For a different controller or compute, set `LABFOUNDRY_GNS3_URL` and
`LABFOUNDRY_GNS3_COMPUTE` before running both commands. For HTTP Basic authentication, set
`LABFOUNDRY_GNS3_USER` and `LABFOUNDRY_GNS3_PASSWORD`.

```bash
labfoundry gns3 check
labfoundry run SCENARIO --guest-config GUEST_INVENTORY
```

Provision actions run before capture. Scheduled events and recurring traffic run on the scenario
timeline. Runs stop their nodes and destroy the project by default; see the
[CLI reference](../reference/cli.md#labfoundry-run) for retention options.

Review the manifest and private journal before distributing
[captures](../guide/captures.md). Connecting a lab to external services requires explicit
[egress configuration](../guide/real-world-egress.md).

If a run fails, check `internal/run.jsonl` for the failing phase. Use `--keep-on-failure` if
you need to inspect the project and its guests after the error.
