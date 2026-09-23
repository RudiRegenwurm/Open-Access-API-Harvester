# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Provider adapters.

Each adapter owns its provider's endpoints, authentication, pagination, response
parsing and error translation. Nothing outside this package may reference a
provider-specific JSON field (MASTER_SPEC sections 3.7 and 40).
"""

from .europepmc import EuropePmcAdapter
from .openalex import OpenAlexAdapter, OpenAlexQuery
from .unpaywall import UnpaywallAdapter

__all__ = ["EuropePmcAdapter", "OpenAlexAdapter", "OpenAlexQuery", "UnpaywallAdapter"]
