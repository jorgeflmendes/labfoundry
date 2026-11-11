# Real-world egress

Real Internet access is explicit and disabled unless the scenario declares a GNS3 NAT attachment.

```yaml
network:
  segments:
    uplink:
      subnet: 192.168.122.0/24

  egress:
    mode: nat
    segment: uplink
    name: labfoundry-nat

  nodes:
    client:
      template: "RF Ubuntu Client VBox"
      guest_profile: linux-vbox
      interfaces:
        wan:
          segment: uplink
          ip: dhcp
          adapter: 0
```

The client attaches to the NAT segment and obtains its address through DHCP.

If a separate lab router sits between internal networks and the NAT uplink, that router must also
perform whatever source-NAT/firewall policy the chosen guest/router OS requires. LabFoundry enables
IPv4 forwarding when requested but does not silently invent guest firewall/NAT rules; use generic
`guest.exec` provisioning for those policy decisions.

LabFoundry creates the built-in GNS3 `nat` node and connects its port to the selected segment. On Linux the GNS3 server normally maps this node to its configured libvirt NAT interface; on other platforms the actual NAT interface is controlled by GNS3 server configuration.

Once the guest has ordinary IP connectivity, LabFoundry does not need special integrations for public services. For example:

```yaml
- at: "09:00:10"
  action: guest.exec
  from: client
  parameters:
    script: 'curl -fsS https://example.com/ >/dev/null'
```

The same primitive can launch Chromium/Playwright, `git`, a package manager, an SMTP client, a custom Python program, or any other application installed in the guest.

!!! warning
    Real-world egress makes the exercise dependent on external availability and can create side effects. Use accounts, domains and recipients controlled for the laboratory. LabFoundry deliberately does not turn on real egress implicitly.

## Cloud nodes

GNS3 Cloud nodes can expose host interfaces more directly than the NAT node. LabFoundry therefore rejects an explicitly declared `template_type: cloud` unless the scenario also sets:

```yaml
network:
  allow_cloud_nodes: true
```

The backend repeats this check after resolving the actual GNS3 template, so omitting `template_type` cannot silently bypass the protection.
