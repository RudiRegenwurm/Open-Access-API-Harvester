# Install Notanda 1.3.0 on Windows

**Version:** Installer Preview 1, 21 September 2026  
**Status:** working guide for the published, currently unsigned installer. The technical install
path is proven. Exact SmartScreen wording and the exact GUI sequence will only be marked as
empirically verified after the current foreign-machine test.

## What you need

- a 64-bit Windows PC;
- internet access;
- your own OpenAlex API key for first use;
- **no Python and no terminal**.

The current Windows installer is not digitally signed yet. Windows may therefore show a
SmartScreen warning on first run. Do **not** disable SmartScreen.

## 1. Download the installer

Open the official technical pre-release:

https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1

Under **Assets**, download **Notanda-1.3.0.msi**.

Published SHA-256:

`119ad7aadad955be20fe70903c29ebd774c6d1470e7a793746a3dd5d2ebe60ef`

The checksum is published for provenance; the normal installation path does not require you to
open a terminal.

## 2. Start the installer

Open **Downloads** and double-click **Notanda-1.3.0.msi**.

### If Windows SmartScreen warns you

The installer is deliberately unsigned at this stage. Microsoft's current documentation describes
the normal override path as **More info** followed by **Run anyway**.

**Important:** the exact warning text and exact order of visible controls in this guide will be
frozen only after the current foreign-machine observation. If your Windows installation offers no
**Run anyway** option, do not weaken security settings. Stop and report the text you see.

## 3. Complete installation

Follow the Notanda installer using its normal defaults.

**Empirical validation point:** the exact installer window/button sequence will be filled from the
foreign-machine test; it is not inferred from the silent CI installation.

When installation finishes, Notanda should be available as an installed application/shortcut.

## 4. Start Notanda

Start **Notanda** from the installed shortcut or Start menu.

Notanda runs locally on your computer and opens its control center in your browser. If the browser
does not open automatically, browse to:

`http://127.0.0.1:8765/`

No terminal window needs to remain open.

## 5. Enter your OpenAlex key on first start

If no OpenAlex key is configured, Notanda takes a clean first start directly to Settings.

1. Obtain your own key at `openalex.org/settings/api`.
2. Copy the key.
3. Paste it into the OpenAlex API-key field under **Settings**.
4. Save the setting.

You do not need to locate or edit a configuration file. The key is stored locally. Never include
it in screenshots or test notes.

## 6. Quick functional check

After saving the key:

1. open **New Harvest**;
2. choose **Conventional Search**;
3. enter a small real research query;
4. preview the results first;
5. then start the harvest.

User-owned data is stored under:

`%USERPROFILE%\Notanda`

This includes configuration, state database, reports and corpus. Installed program files and user
data are kept separate.

## 7. Close and reopen later

Close Notanda in the normal desktop way. Next time, use the installed Notanda shortcut again.
Python, virtual environments and `pip` are no longer part of the normal user path.

## 8. Uninstall

Open **Windows Settings → Apps**, find **Notanda**, and uninstall it.

Uninstalling the application is intended to preserve
`%USERPROFILE%\Notanda`, including the user's corpus and provenance records.

## If something differs from this guide

Do not improvise and do not disable Windows security controls. Record:

- Windows version and display language;
- the step in this guide;
- the exact message text;
- all visible button labels;
- a screenshot if possible, without API keys or other credentials.

The accompanied beta contact remains **beta@notanda.io**.
