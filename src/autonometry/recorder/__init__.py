"""The recorder: ``autonometry record --config FILE`` (README, "Recorder").

It receives control events from connectors over a local Unix datagram socket and writes a sealed,
crash-safe log (definitions.md sections 8 and 10). It never sits in a control loop: connectors
send fire-and-forget datagrams and carry on. Every module except :mod:`.service` runs on any
platform; the service itself needs Linux or macOS.
"""
