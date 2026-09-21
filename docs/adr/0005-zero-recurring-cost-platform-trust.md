# ADR 0005 — Zero-recurring-cost platform trust policy

- **Status:** Accepted
- **Date:** 2026-09-21
- **Supersedes:** the signing/notarization decisions and related acceptance criteria in ADR 0004
- **Scope:** Windows code signing, macOS Gatekeeper handling, Linux signing policy
- **Constraint:** no recurring paid signing or developer-program subscription
- **Not in scope:** brand/website/channel work, MCP/API work, public beta/release authorization

## Context

ADR 0004 selected Briefcase and treated public-trust Windows Authenticode signing plus Apple
Developer ID signing/notarization as production targets.

The project owner has now established a stronger operating constraint: Notanda must not adopt a
solution with recurring signing costs.

Current official platform information confirms the resulting asymmetry:

- SignPath Foundation offers free code signing for qualifying Open Source projects, subject to its
  project and process conditions.
- Apple Developer ID distribution and notarization are benefits of the Apple Developer Program,
  whose individual/standard membership carries a recurring annual fee. The project owner declines
  that membership.
- macOS still permits a user to make a one-time security exception for software from an unknown
  developer, subject to local/managed security policy.

## Decision

### Windows

Use **SignPath Foundation** as the only planned public-trust Windows signing route.

- Maintain the public `CODE_SIGNING_POLICY.md` required by SignPath Foundation.
- Rudolf Kiechle is the current author/committer, reviewer for non-committer changes, and release
  approver.
- Every production signing request requires Rudolf Kiechle's explicit manual approval.
- Use GitHub Actions as the intended trusted build system with origin verification after SignPath
  acceptance.
- Do not purchase an OV/EV code-signing certificate as a fallback.
- If SignPath Foundation rejects or later discontinues the project, Windows remains unsigned until
  another zero-recurring-cost trust path is found or this ADR is explicitly revisited.

The single-maintainer arrangement is documented accurately: it centralizes responsibility but does
not create independent human review. The policy must not describe this as separation of duties.

### macOS

Do **not** enroll in the paid Apple Developer Program solely to obtain Developer ID signing and
notarization.

Continue to package the macOS application with the current ad-hoc signature for internal/beta
distribution and document the Gatekeeper exception procedure in `INSTALL-MACOS.md`.

Primary user procedure after a blocked first launch:

`System Settings → Privacy & Security → Security → Open Anyway → Open`

A Control-click/right-click → Open route may work on some systems, but the Apple-documented
Privacy & Security route is the canonical instruction.

This is an explicit exception to the original "download, double-click, done" product goal. Without
Developer ID/notarization, the macOS first-run trust warning cannot be eliminated; it can only be
made understandable and bounded. Managed Macs may prohibit the override entirely.

### Linux

Keep the existing native package path. No additional paid signing infrastructure is introduced.
The current package/install/uninstall validation remains the technical baseline.

## SignPath eligibility gate

> **Update:** This gate is superseded by ADR 0006, which explicitly authorizes a public unsigned
> Installer Preview for the sole purpose of satisfying the published release condition.

SignPath Foundation's published conditions currently state that a qualifying Open Source project
must already be **released in the form that should be signed** and that functionality must be
documented on its download page/app-store entry.

The Notanda repository currently states the opposite: it is a source publication and intentionally
offers **no public installer or downloadable release artifact**.

Therefore the SignPath application is **prepared but not truthfully submittable as eligible yet**
without changing an existing project boundary. This ADR does not authorize creating a public
unsigned installer, public beta download, tag or GitHub Release merely to satisfy the SignPath
condition.

The application may be submitted once either:

1. SignPath confirms that the current source-only/restricted-beta state is acceptable despite the
   published "released" condition; or
2. Rudolf Kiechle separately authorizes a public Windows installer/download path that satisfies the
   condition.

## Consequences and trade-offs

### Positive

- No recurring signing/developer-program cost is introduced.
- Windows retains a credible path to public-trust Authenticode signatures with verifiable build
  provenance.
- macOS remains installable without Python, terminal use or a paid Apple account.
- The user's corpus remains independent of platform trust vendors.

### Negative

- SignPath acceptance is discretionary and not guaranteed.
- A one-person project cannot provide independent human separation between author, reviewer and
  release approver; the control is explicit manual release approval plus verifiable build origin.
- macOS Gatekeeper friction remains and weakens the "non-technical user can simply run it" goal.
- macOS users receive less platform-level identity assurance than with Developer ID/notarization.
- Managed Macs may block the override, making this zero-cost route unusable in some institutional
  environments.

## Acceptance criteria

### Windows

1. SignPath Foundation accepts the project, or the status remains explicitly "signing pending".
2. The public Code signing policy remains accurate.
3. Production signing uses an origin-verified trusted build, not an unverifiable local binary.
4. Every release-signing request has an explicit manual approval.
5. Signed MSI/executable passes Authenticode verification.
6. Signed installer is tested on a clean non-developer Windows machine.

### macOS

1. DMG/app is built reproducibly from the recorded source commit.
2. SHA-256 is published with the distributed artifact.
3. A non-developer Mac can install and launch it without Python or Terminal.
4. When Gatekeeper blocks launch, the documented one-time exception works on an unmanaged test Mac.
5. The documentation states clearly that the build is not Developer-ID-signed or notarized.
6. No claim of equivalent trust to a notarized application is made.

### Linux

Existing ADR 0004 package lifecycle acceptance remains unchanged, except no paid signing service is
required.

## Exit path

This decision is reversible. If a future zero-cost notarization/signing program becomes available,
or if the recurring-cost constraint changes, create a new ADR and restore platform-native trust
signing without changing the application's data/corpus contract.

## Sources checked 2026-09-21

- SignPath Foundation conditions: https://signpath.org/terms.html
- SignPath Foundation application page: https://signpath.org/apply.html
- SignPath project/signing-policy documentation: https://docs.signpath.io/projects
- Apple Developer Program enrollment/fees: https://developer.apple.com/programs/enroll/
- Apple membership comparison (Developer ID/notarization): https://developer.apple.com/support/compare-memberships/
- Apple Gatekeeper exception guidance: https://support.apple.com/de-de/102445
