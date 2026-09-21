# ADR 0004 — Standalone desktop packaging and installer path

- **Status:** Accepted; signing/notarization section superseded by ADR 0005
- **Date:** 2026-09-21
- **Superseded in part by:** [ADR 0005 — Zero-recurring-cost platform trust policy](0005-zero-recurring-cost-platform-trust.md)
- **Scope:** Packaging, first-run startup, local writable paths, code signing/notarization
- **Not in scope:** Brand/website work, MCP/API expansion, public beta release

## Context

Notanda 1.3.0 is currently distributed as the Python package `oa-harvester`. A user must have
Python available, create or use an environment, and install the wheel before starting the local
web control center. Two independent installation attempts on 2026-09-20 showed that this is a
real product barrier for otherwise suitable beta users.

The reviewed public 1.3.0 source already contains a browser-based control center and allows the
OpenAlex API key to be entered and saved through Settings. However, the current `serve` entry
point defaults to `harvester.json` in the process working directory, while default corpus,
state, and report paths are also relative paths. A desktop installer cannot rely on an arbitrary
working directory or on write access to the installation directory.

The packaging layer must therefore solve two distinct problems:

1. ship Python and dependencies so the end user does not install Python or use a terminal; and
2. launch Notanda with stable per-user writable paths and a first-run setup path.

## Decision

### 1. Packaging tool

Use **BeeWare Briefcase** as the primary packaging tool for the first production-quality
standalone application.

Reasons:

- it bundles a Python runtime with Windows and macOS applications;
- it produces native installable formats directly: Windows MSI, macOS DMG/PKG, and Linux native
  system packages/Flatpak (with AppImage available but not preferred);
- it has first-class packaging hooks for code signing, and on macOS supports signing and
  notarization as part of packaging;
- it can package the existing Python codebase without introducing a second native GUI framework;
- it keeps packaging configuration in `pyproject.toml`, which is reproducible and reviewable.

**PyInstaller** remains the fallback if Briefcase cannot package the existing browser/server
application reliably. PyInstaller is mature and produces standalone executables, but a separate
installer tool would still be required for MSI/EXE, DMG/PKG, and Linux packaging.

**Nuitka** is not selected for V1. It can now create standalone builds and installers on Windows,
macOS, and Linux, but its installer support is comparatively new and it adds a C compiler/build
toolchain without a demonstrated need for native compilation or performance gains in Notanda.

### 2. Platform order

Implement and validate in this order:

1. **Windows x86-64 — MSI**
2. **macOS — signed/notarized app in DMG** (PKG only if installer semantics are needed)
3. **Linux — native package first** (initially Debian/Ubuntu `.deb`); Flatpak may follow.

AppImage is not the primary Linux target because current Briefcase documentation explicitly
discourages AppImage distribution in favor of system packages or Flatpak.

Windows comes first because the current beta path and the strongest observed installation pain
are on Windows, and MSI provides the cleanest double-click installation/uninstallation path.

### 3. Desktop launcher and writable data

Do not change the established CLI semantics merely to accommodate installers.

Add a small packaged-app launcher that starts the existing local web control center with an
explicit per-user Notanda data root. The packaged application must never depend on its current
working directory and must never write into Program Files, the macOS app bundle, or another
installation directory.

For V1, use a visible per-user root:

- Windows: `%USERPROFILE%\\Notanda`
- macOS: `~/Notanda`
- Linux: `~/Notanda`

with at least:

- `config/harvester.json`
- `state/harvester.sqlite3`
- `reports/`
- `corpus/`

This keeps the user's corpus easy to locate and copy while separating writable data from the
installed application. Existing CLI overrides and explicit config paths remain supported.

### 4. First-run OpenAlex setup

On first packaged launch, if no OpenAlex key is configured, open the control center directly in
the setup/settings flow and request the key there. The key is stored locally by the application;
the user must not locate or edit a configuration file manually.

The existing settings API and secret-redaction behavior should be reused. V1 does not add an OS
credential-vault dependency unless testing shows a concrete need. The config file should be
written with restrictive permissions where the platform allows this.

### 5. Signing and notarization

Unsigned installers are acceptable only for internal packaging experiments, never as the target
beta distribution.

**Windows**

- Sign the application binaries and MSI with a publicly trusted Authenticode identity.
- First choice for this MIT-licensed open-source project: apply for SignPath Foundation's free
  OSS signing service if the project meets its acceptance conditions.
- Fallback: obtain a conventional OV code-signing certificate.
- Microsoft's Artifact Signing is not the primary route for the current individual developer
  setup: Public Trust enrollment for individuals is currently limited to the US and Canada,
  while EU availability applies to organizations.
- Signing reduces trust friction but does not guarantee immediate SmartScreen reputation for a
  new binary/certificate; early downloads may still see reputation warnings.

**macOS**

- Enroll in the Apple Developer Program.
- Sign the app with Developer ID Application.
- Use hardened runtime where required.
- Submit every release for Apple notarization and staple the ticket to the distributed DMG/PKG.
- Apple currently charges USD 99/year for the Developer Program (regional price may vary).

**Linux**

- Prefer native package signing for the selected distribution format.
- For Debian/Ubuntu packages, use a reproducible GPG-signed release path.
- Treat AppImage signing as secondary because AppImage is not the selected primary format.

### 6. Build topology

Build each platform on that platform (or an officially supported CI runner for that platform).
Do not treat the packaging system as a cross-compiler.

A later CI workflow may produce unsigned test artifacts on all three operating systems. Release
signing credentials must remain outside the repository and be supplied only by the signing
service/CI secret store.

## Alternatives considered

### PyInstaller + separate installer builders

**Advantages:** mature freezer, small conceptual change, proven support for Windows/macOS/Linux.

**Rejected for V1 because:** it solves freezing but not the whole distribution problem. Notanda
would still need and maintain a second installer stack per platform.

### Nuitka

**Advantages:** standalone/onefile output, compilation, and current native installer creation
(NSIS on Windows, DMG on macOS, AppImage on Linux).

**Rejected for V1 because:** native compilation adds build complexity that does not solve a
current Notanda problem, and the integrated installer functionality is newly introduced.

### Continue wheel distribution with better documentation

Rejected. The observed beta-installation friction is technical, not primarily a documentation
problem.

## Acceptance criteria

The installer work is complete only when all of the following are demonstrated:

1. A clean target machine without Python can install Notanda by opening one downloaded artifact.
2. Launching Notanda requires no terminal.
3. The local control center opens automatically.
4. First launch without an OpenAlex key takes the user directly to setup and the key can be
   entered and saved from the UI.
5. All writable data is under the user's Notanda data root, not the installation directory or
   arbitrary current working directory.
6. A real search and at least one OA acquisition complete successfully.
7. The acquired file passes Notanda's existing integrity verification.
8. Uninstall removes the application while preserving the user's corpus unless the user
   explicitly chooses otherwise.
9. Production Windows artifacts are Authenticode-signed.
10. Production macOS artifacts are Developer-ID-signed, notarized, and accepted by Gatekeeper.
11. The exact source commit, build tool versions, artifact SHA-256 values, and signing/notary
    status are recorded in the handoff manifest.

## Effort estimate

Engineering effort, excluding external identity-verification/enrollment waiting time:

- Windows unsigned functional MSI + first-run path: **1–2 focused days**
- Windows signing integration and clean-machine validation: **0.5–1.5 days**
- macOS packaging + signing/notarization + Intel/Apple-Silicon validation: **1–2 days**
- Linux native package + clean-machine validation: **0.5–1.5 days**
- Cross-platform CI hardening and final handoff evidence: **1–2 days**

Expected total: approximately **4–8 focused engineering days**, with signing enrollment capable
of extending calendar time.

## Exit / fallback path

Time-box the first Windows Briefcase packaging experiment. If a clean Windows MSI cannot launch
the existing Notanda control center reliably after one focused engineering day, stop rather than
debug Briefcase indefinitely. Fall back to **PyInstaller onedir + WiX/another MSI builder** while
keeping the launcher, data-path, first-run, signing, and acceptance decisions in this ADR.

## References checked on 2026-09-21

- Briefcase platform/configuration documentation: https://briefcase.beeware.org/
- PyInstaller documentation: https://pyinstaller.org/
- Nuitka user manual: https://nuitka.net/user-documentation/user-manual.html
- Apple Developer ID / notarization documentation: https://developer.apple.com/developer-id/
- Microsoft SmartScreen reputation guidance:
  https://learn.microsoft.com/windows/apps/package-and-deploy/smartscreen-reputation
- Microsoft Artifact Signing quickstart:
  https://learn.microsoft.com/azure/artifact-signing/quickstart
- SignPath Foundation OSS signing conditions: https://signpath.org/terms.html
