# Exceptions

LabFoundry exposes a small exception hierarchy rooted at `LabFoundryError`. Backend-specific failures are separated from scenario, evidence and PCAP failures so callers can make reliable recovery decisions.

::: labfoundry.errors
    options:
      members: true
