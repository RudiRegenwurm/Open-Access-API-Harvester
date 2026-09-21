# Install Notanda 1.3.0 on macOS

**Version:** Installer Preview 1, 21 September 2026  
**Status:** working guide for the published app, which is ad-hoc signed and not notarized. Technical
app startup is proven. Exact Gatekeeper wording will only be marked as empirically verified after
the current foreign-machine test.

## What you need

- a Mac compatible with this build;
- internet access;
- your own OpenAlex API key for first use;
- **no Python and no terminal**.

Notanda is currently **not signed with an Apple Developer ID and is not notarized**. macOS is
therefore expected to show a Gatekeeper warning on first open.

## 1. Download Notanda

Open the official technical pre-release:

https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1

Under **Assets**, download **Notanda-1.3.0.dmg**.

Published SHA-256:

`928cbe2c1a031c00f5033cd75130d458f3a6c5a6122b5bd126ea549284fa2210`

The normal installation path does not require Terminal.

## 2. Copy the app to Applications

1. Open **Notanda-1.3.0.dmg**.
2. Use Finder to copy **Notanda.app** to **Applications**.
3. Open **Applications**.
4. Double-click **Notanda**.

## 3. Allow the Gatekeeper exception once

On first open, macOS may block the app because it is not Developer-ID signed and notarized.

**Empirical validation point:** the exact first alert text will be frozen only after the current
foreign-machine test. Apple's current documentation shows more than one possible alert variant, so
this guide does not pretend one generic message is the observed Notanda message.

After the blocked launch attempt, use the ADR 0005 path:

1. Open **System Settings**.
2. Open **Privacy & Security**.
3. Scroll down to **Security**.
4. Find the message about Notanda.
5. Click **Open Anyway**.
6. Authenticate with Touch ID or your Mac login password if macOS asks.
7. The warning appears again. Click **Open**.

macOS then saves Notanda as an exception and later launches should normally work with a
double-click.

If **Open Anyway** is not available, do not disable Gatekeeper and do not use a Terminal command to
bypass it. Organization-managed Macs can prevent this exception.

## 4. Notanda starts locally

After the exception is approved, Notanda starts locally and opens the control center in your
browser.

If the browser does not open automatically, browse to:

`http://127.0.0.1:8765/`

No Terminal window needs to stay open.

## 5. Enter your OpenAlex key on first start

If no OpenAlex key is configured, a clean first start takes you to Settings.

1. Obtain your own key at `openalex.org/settings/api`.
2. Copy it.
3. Paste it into the OpenAlex API-key field under **Settings**.
4. Save the setting.

There is no configuration file to find or edit. The key is stored locally and must not appear in
screenshots or test notes.

## 6. Quick functional check

After saving:

1. open **New Harvest**;
2. choose **Conventional Search**;
3. enter a small real research query;
4. preview first;
5. then start the harvest.

Notanda stores user-owned data under:

`~/Notanda`

including configuration, state database, reports and corpus.

## 7. Quit and uninstall

Quit Notanda like a normal Mac application.

To uninstall, remove **Notanda.app** from **Applications**. The user-owned `~/Notanda` directory
is preserved so corpus and provenance records are not silently deleted.

## If something differs from this guide

Do not change macOS security settings beyond the documented **Open Anyway** exception. Record:

- Mac model/processor and macOS version;
- system display language;
- the step in this guide;
- exact alert text and all visible buttons;
- a screenshot if possible, without credentials.

The accompanied beta contact remains **beta@notanda.io**.
