# GNS3 Controller API contract

LabFoundry 0.2 supports the **GNS3 Controller API v2 on GNS3 Server `>=2.2.61,<2.3.0`**. The
implementation is checked against the 2.2.61 contract and intentionally refuses unknown 2.3+
semantics rather than guessing compatibility. Local PCAP streaming uses a `/v2/compute/` route
through the configured controller origin.

The client validates that its base URL is an `http`/`https` controller origin without embedded
credentials, arbitrary paths, queries or fragments.

## Controller routes consumed

- `GET /v2/version`
- `GET /v2/computes/{compute_id}`
- `GET /v2/templates`
- `POST /v2/templates`
- `POST /v2/projects`
- `DELETE /v2/projects/{project_id}`
- `POST /v2/projects/{project_id}/templates/{template_id}`
- `POST /v2/projects/{project_id}/nodes`
- `GET /v2/projects/{project_id}/nodes`
- project/node start/stop routes
- `POST /v2/projects/{project_id}/links`
- `PUT /v2/projects/{project_id}/links/{link_id}`
- `GET /v2/projects/{project_id}/links/{link_id}/available_filters`
- link start/stop capture and `/pcap`
- `GET /v2/compute/projects/{project_id}/{node_type}/nodes/{node_id}/adapters/{adapter}/ports/{port}/pcap` for local capture streams
- project notifications WebSocket
- project snapshot create/list/restore/delete routes

## Built-in nodes

Every logical segment becomes a built-in `ethernet_switch`. Explicit real-world egress creates a
built-in `nat` node through the normal project-node Controller route and links it to the selected
segment. Cloud templates are denied by default because they can expose host interfaces directly.
When `--nat-compute` is set, only the NAT node is placed on that connected compute; GNS3 carries
the cross-compute link while guest nodes, switches and capture endpoints stay on the primary
compute.

Incus nodes use managed cloud nodes containing only explicit UDP ports. These are created directly
through the project-node route; no host Ethernet/TAP interface is selected. Unconnected Incus nodes
use unconnected switch placeholders because an empty GNS3 cloud can populate host interfaces.
Their normal links and captures still use the controller routes above.

## Link filters

`/available_filters` is parsed into typed objects containing filter type and parameter metadata
(name/type/minimum/maximum/unit). Runtime link updates validate supplied filter arguments against
what the connected controller advertises before issuing a `PUT`.

## PCAP transfer

The compatibility `download_pcap()` method returns bytes, while the evidence path opens the live
PCAP stream, stops the capture over a separate HTTP connection, then drains the response to an
atomic temporary file and `fsync`s before rename. For the local compute, LabFoundry uses the direct
compute stream because the GNS3 2.2.61 controller proxy emits an invalid chunked response on Windows.
GNS3 removes the link's PCAP endpoint after capture stops, while an open live stream does not reach
EOF until capture stops, so this ordering is required.
Streaming avoids buffering multi-gigabyte captures in Python memory.
If shutdown ends in the middle of the final PCAP record, the raw source is retained and evidence
export fails. LabFoundry does not silently discard the incomplete tail.

## VirtualBox

`VirtualBoxTemplateCreate` mirrors the supported upstream fields. Optional custom-adapter values are
omitted rather than serialized as JSON `null`. Concrete adapter/port inventory returned for created
nodes is checked against the compiled plan before links are built.

Official upstream documentation remains the source of truth for controller behavior; LabFoundry's
real-host integration tests are the release gate for actual GNS3 + VirtualBox behavior.
