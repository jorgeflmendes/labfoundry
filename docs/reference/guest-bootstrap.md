# Guest bootstrap reference

Guest bootstrap YAML describes how to prepare a reusable Linux or Windows base image. It is separate
from the scenario DSL: image preparation happens once, while a scenario can create many linked
clones from the resulting template.

## Common fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `version` | string | no | Bootstrap format version. The current value is `"0.1"`. |
| `os` | `linux` or `windows` | yes | Selects the generated first-boot format. |
| `hostname` | string | yes | Hostname assigned during first boot. Windows names are limited to 15 characters. |
| `users` | list | yes | Local accounts to create. Names must be unique. |
| `packages` | list of strings | no | Distribution packages on Linux or exact winget IDs on Windows. |
| `services` | list of strings | no | systemd units on Linux or service names on Windows. |
| `commands` | list of strings | no | Shell or PowerShell commands run in order at first boot. |
| `guest_additions` | boolean | no | Install and start VirtualBox Guest Additions. Defaults to `true`. |

Unknown fields are rejected. Package, service, and command entries must be non-empty and unique.

## Users

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | — | Local account name. |
| `password` | `generated` or `locked` | `generated` | Generate a random password or disable password login. |
| `groups` | list of strings | `[]` | Supplementary groups. Used by Linux images. |
| `administrator` | boolean | `false` | Adds passwordless sudo on Linux or the Administrators group on Windows. |
| `control` | boolean | `false` | Selects the account used by VirtualBox Guest Additions at runtime. |
| `shell` | string or null | null | Login shell for a Linux account. |

Exactly one user must set `control: true`, and that user must use a generated password. The command
writes one password file per generated account below the local bundle's `credentials/` directory.
Only the control account is referenced by `guest_inventory.yaml`. On Windows, the control account
must also set `administrator: true` because first-logon provisioning requires elevation.

## Linux

Linux bootstrap requires a cloud-init-enabled image. `package_update` controls whether package
metadata is refreshed and defaults to `true`. Packages are installed through cloud-init's native
package module. Services are enabled and started with `systemctl` before the custom commands run.

For historical Ubuntu images, `apt_snapshot: "20250904T235959Z"` selects the package archive at
that UTC instant. This requires `package_update: true`; index download errors fail the update rather
than falling back to cached indexes. The seed writes `APT::Snapshot` before cloud-init installs
packages. This needs snapshot support in APT and the configured repositories; Ubuntu 24.04 supports it for its official
repositories. See the [Ubuntu snapshot service](https://snapshot.ubuntu.com/).

Start from an image published before the scenario date. A snapshot does not downgrade packages
already installed in a newer image, or pin downloads made by custom commands and third-party
repositories. Those need separately dated versions. Keep historical guests isolated from public
services during simulation, since their packages lack later security fixes.

The output includes readable `user-data` and `meta-data`, a VFAT NoCloud image, and a VMDK descriptor.
Attach `seed.vmdk` for the first boot, wait for cloud-init to finish, then detach it before taking the
base snapshot.

## Windows

The Windows output contains `Autounattend.xml`, `bootstrap.ps1`, and a
`$OEM$/$$/Setup/Scripts/LabFoundry.ps1` copy. Windows Setup copies `$OEM$/$$` into `%WINDIR%`, so the
first-logon command has a stable script path. Packages use exact winget IDs.
Services are set to automatic startup and started before custom commands run. A locked account is
created with a temporary random password and disabled by the bootstrap script.

Use the generated directory as a Windows Setup configuration set and attach the VirtualBox Guest
Additions media. The control account logs in once to run the copied bootstrap script.

## Command

```bash
labfoundry guest bootstrap BOOTSTRAP_YAML INVENTORY_TEMPLATE --output BUNDLE_DIRECTORY
```

The output directory must not already exist. Keep it outside the repository: generated first-boot
media contains account credentials. Remove or secure the media after the template has been prepared.
