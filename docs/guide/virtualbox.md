# VirtualBox integration

## Upstream contract

GNS3 exposes `virtualbox` as an official template/node type. LabFoundry mirrors the VirtualBox
template fields it consumes, including `vmname`, RAM, linked clones, adapter count/type, headless
mode, close behavior, console settings and custom adapters.

The VM named by `vmname` must already be registered in VirtualBox on the selected GNS3 compute.

## Linked and non-linked runtime VM names

For a linked clone, GNS3 creates/registers a clone using the GNS3 node name. For a non-linked
VirtualBox node, the effective VirtualBox machine remains the template `vmname`. LabFoundry records
the runtime guest identifier returned/resolved during deployment and passes the correct machine name
to `VBoxManage guestcontrol`.

## Guest Additions transport

The `virtualbox` guest profile uses the documented `VBoxManage guestcontrol` interface and therefore
requires working VirtualBox Guest Additions inside the VM.

Supported operations include command execution, directory creation, `copyto`/`copyfrom`, network
provisioning and tracked processes. `labfoundry guest configure` prompts for the guest password and
writes it to a separate local password file. The inventory references that file and VBoxManage sees
only a temporary `--passwordfile`; the password is not put in the scenario or command-line
arguments. POSIX hosts restrict that file to mode 0600. On Windows, access follows the temporary
directory's ACLs; protect the inventory and persistent password file with suitable ACLs too.

Prepare an inventory template with the VirtualBox profile names referenced by your scenario,
the guest OS and the correct `interface_map`. See [Guest execution](guest-execution.md) for the
inventory fields. Configure credentials interactively:

```bash
labfoundry guest configure INVENTORY_TEMPLATE --output ~/.labfoundry/guest_inventory.yaml
```

## First-boot bootstrap

Image preparation can be described in a separate bootstrap YAML. It supports multiple local users,
generated or locked passwords, groups, an administrative account, packages, services, arbitrary
first-boot commands, hostname assignment, and optional Guest Additions installation. Exactly one
generated-password user must have `control: true`; that account is written into the guest inventory
used by the runtime.

Write your bootstrap YAML using the [bootstrap reference](../reference/guest-bootstrap.md).

Create the local bundle before making the GNS3 template:

```bash
labfoundry guest bootstrap BOOTSTRAP_YAML INVENTORY_TEMPLATE --output ~/.labfoundry/lab-client
```

Generated passwords are stored as separate files below `credentials/`. They are not written to the
bootstrap specification, guest inventory, scenario, compiled plan, or command line. The generated
first-boot media necessarily contains the credentials needed to create the accounts; keep the bundle
private and detach the media after preparation.

For a new Ubuntu cloud image, the older convenience command creates the built-in default profile:

```bash
labfoundry guest bootstrap-linux INVENTORY_TEMPLATE --output ~/.labfoundry/bootstrap/linux
```

It creates a random `labfoundry` account, a matching local guest inventory, and
`seed/seed.vmdk`. Attach that disk to the powered-off cloud-init image, boot it once, then confirm
that `cloud-init`, `VBoxService`, and SSH completed. Power it off, detach the seed disk, and take
the base snapshot used by the GNS3 linked-clone template. Use the generated `guest_inventory.yaml`
for scenario runs.

The seed installs Python 3, sudo, OpenSSL, OpenSSH, `arp-scan`, Nmap, and the distribution's
`virtualbox-guest-utils` package. It is for a fresh, cloud-init-enabled image only; it does not
modify an already-initialized installation.

For a fresh Windows installation, use `os: windows`. Packages are installed with `winget`. The
unattended file creates all users, logs the control account in once, and runs the bootstrap script for
Guest Additions, packages, services, and commands. The
[bootstrap reference](../reference/guest-bootstrap.md#windows) describes the Windows fields.

The older convenience command generates the built-in Windows profile:

```bash
labfoundry guest bootstrap-windows INVENTORY_TEMPLATE --output ~/.labfoundry/windows
```

Use the generated `unattend/` directory as a Windows Setup configuration set: it contains
`Autounattend.xml` and the `$OEM$/$$/Setup/Scripts` copy layout. Attach the VirtualBox Guest
Additions media as well. `bootstrap.ps1` is also written at the bundle root for inspection. Existing
Windows installations still need one owner-authorized console bootstrap.

## Linked-clone portability

Linked clones are useful for generating many disposable instances. GNS3 creates the linked base
snapshot when required, but upstream project export does not make VirtualBox linked-clone projects
portable in the same way as self-contained emulator images. LabFoundry therefore treats registered
VirtualBox base VMs as external lab assets and records resolved template metadata in run manifests.

## Evidence isolation

VirtualBox Guest Additions control is host/VirtualBox control-plane activity, not traffic sent over
the simulated GNS3 NICs. It is therefore the preferred full-VM provisioning transport when the
objective is to avoid management packets contaminating captured data-plane evidence.

SSH remains available when an explicit management path is desired; in that case the scenario author
is responsible for ensuring management traffic is outside student capture points.
