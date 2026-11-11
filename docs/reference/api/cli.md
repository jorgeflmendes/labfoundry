# CLI implementation API

The public command-line surface is documented for operators in the [CLI reference](../cli.md). This page documents the Python callbacks and Typer application objects that implement that surface.

The callbacks are public for introspection and testing, but applications embedding LabFoundry should normally use the compiler/backend APIs directly rather than calling Typer callbacks.

::: labfoundry.cli.main
    options:
      members: true
