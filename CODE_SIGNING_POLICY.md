# Code signing policy

**Project:** Notanda / Open-Access API Harvester  
**Repository:** https://github.com/RudiRegenwurm/Open-Access-API-Harvester  
**Maintainer:** Rudolf Kiechle (`@RudiRegenwurm`)  
**Scope of this policy:** Windows release artifacts

**Free code signing provided by SignPath.io, certificate by SignPath Foundation.**

This policy describes how Notanda will use SignPath Foundation code signing if the project is
accepted into the free Open Source program. It does not claim that the application has already
been accepted or that the current unsigned MSI is a signed release.

## Team roles

Notanda currently has one project maintainer. The same maintainer fills the applicable SignPath
project roles; this is documented explicitly rather than implying organizational separation that
does not exist.

- **Author / committer:** Rudolf Kiechle (`@RudiRegenwurm`).
- **Reviewer:** Rudolf Kiechle reviews changes proposed by non-committers before they are merged.
  Direct maintainer commits are not represented as independently reviewed.
- **Release approver:** Rudolf Kiechle. Every release-signing request requires his explicit manual
  approval. No unattended production signing is permitted.

If additional maintainers join the project, this page must be updated before their repository or
SignPath permissions are used for signing.

Multi-factor authentication is required for the GitHub account and, once provisioned, for the
SignPath account.

## Build and origin policy

Release signatures may only be applied to binaries produced from this repository by the configured
trusted build system.

The intended release-signing lane is:

1. source and build workflow are committed to the public repository;
2. GitHub Actions builds the Windows installer from that source;
3. SignPath origin verification must identify repository URL, branch/tag, commit and build job;
4. a release-signing request is created for the resulting artifact;
5. Rudolf Kiechle manually approves or denies that request;
6. the signed artifact is verified and its SHA-256 and signing state are recorded in the Notanda
   handoff manifest.

Manual local uploads are not the normal release path. A release artifact must not be signed if its
source provenance cannot be traced to the reviewed repository build.

The signing policy should be restricted to the project's release branch/tag convention once
SignPath onboarding is complete. The exact SignPath-side configuration will be recorded after
acceptance rather than invented in advance.

## Artifact scope

Only Notanda artifacts built from the project's own source and build scripts may be submitted for
release signing. Upstream Open Source dependencies may be included in an installer under their own
licenses but are not to be re-signed as if they were Notanda-owned binaries.

Product and version metadata in signed files must consistently identify Notanda and the release
version.

## User privacy and network access

Notanda is a local application and does not include application telemetry in the reviewed source.
It makes network requests when necessary to perform functions requested by the user, including
scientific discovery, verification and Open Access acquisition through configured providers such
as OpenAlex, Europe PMC and Unpaywall, and optional services explicitly configured by the user.

Credentials are stored locally and are redacted from logs, reports, provenance and settings
responses according to the application's existing secret-handling behavior.

## Installation and system changes

The Windows installer installs the Notanda application and creates normal application shortcuts
where configured. Runtime data is stored under the user's own Notanda data root rather than the
installation directory.

Uninstallation is provided through the Windows installer mechanism. Uninstalling the application
must preserve the user's corpus and other user-owned Notanda data unless the user explicitly
chooses to remove them.

Notanda does not intentionally disable operating-system security controls.

## Release verification

For each signed Windows release, the handoff record must include at least:

- exact source commit;
- GitHub Actions build/run identifier;
- unsigned and/or signed artifact SHA-256 values as applicable;
- SignPath signing-request identity when available;
- Authenticode verification result;
- clean-machine install/start/uninstall result.

A signing request must not be approved merely because a build succeeded; the release approver is
responsible for confirming that the intended source/version and required acceptance evidence match
the signing request.

## Incident handling

If a signed Notanda release is suspected of policy violation, compromise, malware, provenance
breakage or unintended content, further release signing is paused while the report is investigated.
The project will cooperate with SignPath Foundation on verification and, if necessary, certificate
or signature revocation procedures.

Project contact: `beta@notanda.io`

## Status

As of 2026-09-21, this policy is prepared for a SignPath Foundation application. A public,
unsigned Installer Preview is authorized to satisfy the Foundation's "released in the form that
should be signed" condition. The preview is not recommended for non-technical users and is not a
SignPath-signed release. No SignPath Foundation certificate or subscription is claimed until
SignPath confirms acceptance.
