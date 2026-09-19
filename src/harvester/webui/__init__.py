"""Local web UI for the Open-Access API Harvester.

A thin control center. Every capability it exposes is executed by the existing core
services (:mod:`harvester.orchestrator`, :mod:`harvester.state`,
:mod:`harvester.verify`), in-process, against the same persistent state the CLI uses.
No harvesting rule is reimplemented here.
"""

from .server import create_server, serve

__all__ = ["create_server", "serve"]
