# Third-party notices

The root MIT license applies to the Notanda / Open-Access API Harvester product
code and documentation except where this file or another file states a different
license.

## Bundled fonts

The following font software is distributed with the local web UI and is not
covered by the root MIT license.

### IBM Plex

Copyright © 2017 IBM Corp. with Reserved Font Name "Plex".

License: SIL Open Font License 1.1. The complete license text is in
`src/harvester/webui/static/LICENSE-IBMPlex-OFL.txt`.

Files:

- `src/harvester/webui/static/ibm-plex-mono-latin-400-normal.woff2`
- `src/harvester/webui/static/ibm-plex-sans-latin-400-italic.woff2`
- `src/harvester/webui/static/ibm-plex-sans-latin-400-normal.woff2`
- `src/harvester/webui/static/ibm-plex-sans-latin-500-normal.woff2`

### Source Serif 4

Copyright 2014–2023 Adobe, with Reserved Font Name "Source".

License: SIL Open Font License 1.1. The complete license text is in
`src/harvester/webui/static/LICENSE-SourceSerif4-OFL.txt`.

Files:

- `src/harvester/webui/static/source-serif-4-latin-wght-italic.woff2`
- `src/harvester/webui/static/source-serif-4-latin-wght-normal.woff2`

## External Python dependencies

The following packages are declared dependencies but are not copied into this
repository. They are installed separately and remain governed by their own
licenses. This list records the declared dependency boundary; the corresponding
project metadata is authoritative for any version actually installed.

| Dependency | Role | Declared license |
| --- | --- | --- |
| `httpx` | runtime HTTP client | BSD 3-Clause |
| `pypdf` | runtime PDF validation | BSD 3-Clause |
| `defusedxml` | runtime hardened XML parsing | Python Software Foundation License |
| `pytest` | optional development/test dependency | MIT |
| `pytest-cov` | optional development/test dependency | MIT |

The Notanda visual identity files have a separate scope described in
[`TRADEMARKS.md`](TRADEMARKS.md).
