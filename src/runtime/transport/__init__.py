"""Transports (DESIGN.md C3) - dumb pipes over the ledger.

A transport normalizes inbound messages into the ledger and delivers outbound
replies from it. It NEVER blocks on agent work (the prior runtime's outage: one
slow op froze the comms loop), and transport semantics NEVER leak into agent
behavior (a chat platform's 'group' classification once made the bot lurk and
drop replies). Both rules are structural here: inbound and outbound are fully
decoupled through the ledger.
"""
from runtime.transport.terminal import TerminalTransport

__all__ = ["TerminalTransport"]
