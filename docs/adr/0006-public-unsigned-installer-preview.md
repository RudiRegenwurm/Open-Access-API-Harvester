# ADR 0006 — Public unsigned Installer Preview for SignPath eligibility

- **Status:** Accepted
- **Date:** 2026-09-21
- **Supersedes:** the "no public installer/download" boundary only where needed for this Installer Preview
- **Related:** ADR 0004 (standalone packaging), ADR 0005 (zero-recurring-cost trust policy)
- **Not in scope:** promoted public beta, outreach to existing testers, brand/website/channel changes, MCP/API work

## Context

SignPath Foundation's published Open Source eligibility conditions require a project to already be
released in the form that should be signed and to describe its functionality on the corresponding
download/release page.

Notanda's standalone installers have already been technically built and validated:

- Windows MSI: install/start/uninstall proven without a separately installed Python on PATH.
- macOS DMG: app/DMG build and first-run local control center proven; ad-hoc signature only.
- Linux DEB: package install/start/uninstall proven on Ubuntu 24.04.

The earlier project boundary deliberately prohibited a public installer. Rudolf Kiechle has now
explicitly relaxed that boundary for one purpose: publish the already built installer artifacts as
a clearly labeled technical preview so the SignPath "released" condition is actually met rather
than described aspirationally.

## Decision

Publish a GitHub **pre-release**:

- **Tag:** `v1.3.0-installer-preview.1`
- **Title:** `Notanda 1.3.0 — Installer Preview 1 (unsigned technical preview)`
- **Release target:** tested installer source commit
  `2039d9e8e73f0b83ca6e78d3902035de8133bf50`
- **Assets:** the exact already-built/tested Windows MSI, macOS DMG and Ubuntu DEB plus
  `SHA256SUMS.txt`.

The release description must state unambiguously:

1. Windows and Linux artifacts are unsigned; macOS is only ad-hoc signed.
2. This is a technical pre-release, not the recommended path for non-technical users.
3. Windows may show SmartScreen warnings; macOS may show Gatekeeper warnings.
4. The recommended accompanied beta path remains exclusively `beta@notanda.io`.
5. The page exists to make the signable installer form publicly available and documented for the
   SignPath Foundation application; it is not the promoted beta page.

Do not send or promote this release to Daniel, Alex, or the four possible additional testers as
part of this action.

## Public repository consistency

Because SignPath requires a code-signing policy on the project's home page, publish a
documentation-only update on `main` that:

- removes the now-false statement that no downloadable installer exists;
- labels the Installer Preview as unsigned/ad-hoc and non-recommended for non-technical users;
- links `CODE_SIGNING_POLICY.md`;
- includes the required sentence:
  **"Free code signing provided by SignPath.io, certificate by SignPath Foundation."**
- publishes `INSTALL-MACOS.md` with the Gatekeeper exception path.

This does not merge the installer implementation branch into `main`.

## SignPath application consequence

Once the GitHub pre-release is publicly visible and its assets are verified, the previously
documented "released" eligibility blocker is considered removed. The regular SignPath Foundation
application should then be submitted using the public release URL and the public code-signing
policy.

If the application form itself cannot be submitted from the available execution environment, do
not claim that it was submitted; preserve all completed evidence and report that specific external
action as blocked.

## Acceptance criteria

1. GitHub Release is publicly visible and marked as a pre-release.
2. Exactly the tested MSI/DMG/DEB bytes are attached; hashes match the recorded values.
3. `SHA256SUMS.txt` is attached.
4. Release notes contain all warning and beta-channel statements above.
5. `main` publicly contains the code-signing policy link and policy page.
6. No promoted beta mailing or tester notification is sent.
7. The SignPath application is either actually submitted or explicitly recorded as blocked by the
   available form-submission capability.

## Evidence

Known artifact provenance before publication:

- Windows workflow run `35586617355`, artifact `10633082147`:
  `Notanda-1.3.0.msi` SHA-256
  `119ad7aadad955be20fe70903c29ebd774c6d1470e7a793746a3dd5d2ebe60ef`.
- macOS workflow run `35586617462`, artifact `10632313176`:
  `Notanda-1.3.0.dmg` SHA-256
  `928cbe2c1a031c00f5033cd75130d458f3a6c5a6122b5bd126ea549284fa2210`.
- Linux workflow run `35586617462`, artifact `10632009430`:
  `harvester_1.3.0-1~ubuntu-noble_amd64.deb` SHA-256
  `37e300384a9ac7949391023b9a4f2509dbe1068e1e8d6e06d9faf180df84d52c`.

## Trade-off

This publication intentionally increases discoverability of unsigned binaries. The mitigation is
to mark the release as a pre-release, keep the warnings prominent, publish exact checksums, avoid
promotion to non-technical testers, and retain `beta@notanda.io` as the recommended accompanied
entry path.
