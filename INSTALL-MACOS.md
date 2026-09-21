# Notanda on macOS — standalone app installation

**Status:** public technical Installer Preview; not recommended for non-technical users  
**Signing:** ad-hoc only; not Apple Developer ID signed and not notarized  
**Terminal required:** no

Notanda intentionally does not require a paid Apple Developer Program membership. As a result,
macOS cannot identify the app as coming from an Apple-verified developer and Gatekeeper may block
the first launch. This is an accepted trade-off of the zero-recurring-cost distribution policy.

## Install

1. Download the Notanda `.dmg` from the trusted Notanda distribution location provided for the
   test/release.
2. Verify the published SHA-256 value before opening the image.
3. Open the `.dmg`.
4. Copy **Notanda.app** to **Applications**.
5. Try to open **Notanda** normally once.

If macOS opens the app, no further step is required.

## If Gatekeeper blocks the first launch

Use Apple's documented exception flow:

1. After the blocked launch attempt, open **System Settings**.
2. Open **Privacy & Security**.
3. Scroll to **Security**.
4. Find the message that Notanda was blocked and click **Open Anyway**.
5. Authenticate with your Mac login credentials if requested.
6. Confirm **Open** in the warning dialog.

Apple documents that this exception control is available for a limited period after the blocked
launch attempt. Once approved, macOS stores an exception and later launches can normally use a
double-click.

A secondary-click / Control-click (often called right-click) on the app followed by **Open** may
also provide an override on some macOS versions and configurations. Do not rely on this route as
the primary instruction; the **Privacy & Security → Open Anyway** procedure above is the current
Apple-documented path.

On a Mac managed by an employer, university or other administrator, policy may disable user
Gatekeeper overrides. In that case the local administrator must permit the application; Notanda
does not attempt to bypass managed security policy.

## First start

Notanda starts its local control center and opens it in the browser. If no OpenAlex API key has
been configured yet, the initial flow opens Settings so the key can be entered and saved locally.

Writable application data is kept under:

`~/Notanda`

including configuration, corpus, state and reports. The application does not require a manually
edited configuration file for the OpenAlex key.

## Security note

An ad-hoc signature is not equivalent to Apple Developer ID signing and notarization. Apple has
not verified the developer identity or notarized this build. Only install Notanda from the
project's trusted distribution channel and compare its SHA-256 value with the published value.

This limitation is deliberate under ADR 0005; it is not represented as the same trust experience
as a notarized Mac application.

## Uninstall

Remove **Notanda.app** from Applications. The user-owned `~/Notanda` data directory is preserved
so that the corpus and provenance records are not silently deleted.
