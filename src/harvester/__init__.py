"""Open-Access API Harvester.

A resumable, idempotent, headless CLI pipeline that discovers Open-Access scholarly
works via OpenAlex Topics, cross-checks them against Europe PMC, falls back to
Unpaywall for OA locations, and stores validated PDF/XML artifacts plus mandatory
JSON sidecars in a flat downstream corpus.
"""

__version__ = "1.3.0"
