# Notanda Installer Preview — macOS foreign-machine Gatekeeper test

**Status:** ready for external execution  
**Audience:** Alex plus up to four additional technically experienced invited testers, if assigned to macOS  
**Distribution:** technical preview only; do not forward to uninvolved third parties  
**Date:** 2026-09-21  
**Artifact:** `Notanda-1.3.0.dmg`  
**SHA-256:** `928cbe2c1a031c00f5033cd75130d458f3a6c5a6122b5bd126ea549284fa2210`  
**Signing:** ad-hoc only; not Developer-ID signed and not notarized  
**Release:** https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1

## Purpose

This test records the real Gatekeeper first-run experience of the current ad-hoc-signed Notanda
application on a Mac that is not a Notanda development machine. The exact alert wording must come
from the tested Mac, not from memory or a generic Apple screenshot. The current invited test group
may be technically experienced; the protocol still uses only the normal Finder/System Settings
path and never a Terminal-based bypass.

The release and every test invitation must state that this is a **technical preview, not the
recommended path for non-technical users**.

Do not disable Gatekeeper and do not use Terminal commands such as `xattr`, `spctl`, or
`sudo` to bypass the warning.

## Before starting

Record:

- Mac model and processor family (Apple silicon or Intel);
- macOS version/build;
- macOS display language;
- browser name and version used for the download;
- whether Notanda has ever been installed on this Mac before;
- whether the Mac is personally managed or controlled by an employer/university/MDM.

## Test sequence

1. Open the public Installer Preview release page:
   https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1
2. Download **Notanda-1.3.0.dmg** in the normal browser.
3. Record any browser/download warning before dismissing it.
4. Open the DMG.
5. Copy **Notanda.app** to **Applications** using Finder.
6. Open **Notanda** by double-clicking it in Applications.
7. At the first Gatekeeper alert:
   - **do not continue yet**;
   - take a screenshot;
   - record the exact title, body text and every button label.
8. Close/dismiss the alert using the normal non-bypass button shown by macOS. Do not use
   Control-click/right-click as the primary route.
9. Open **System Settings → Privacy & Security**.
10. Scroll to **Security** and find the Notanda block. Before clicking anything:
    - take a screenshot;
    - record the exact text;
    - record the exact label of the override button.
11. Follow the ADR 0005 route: click **Open Anyway / Dennoch öffnen**.
12. If macOS asks for Touch ID or the Mac login password, record that authentication was requested
    (never record the password).
13. When the warning appears again:
    - take a screenshot;
    - record the exact text and button labels;
    - click **Open / Öffnen**.
14. Confirm whether Notanda starts, the browser opens automatically and
    `http://127.0.0.1:8765/` becomes reachable.
15. On a clean first start, confirm whether Settings/setup is shown because no OpenAlex API key is
    configured. Do not include an API key in screenshots or notes.
16. Quit Notanda in the normal desktop way.
17. Remove **Notanda.app** from Applications. Confirm whether the user-owned `~/Notanda`
    directory remains.

## Stop conditions

Stop and record the screen instead of improvising if:

- **Open Anyway / Dennoch öffnen** does not appear after the first blocked launch;
- the Mac is managed and policy prevents the override;
- macOS says the app is damaged rather than merely unidentified/not notarized;
- the app is incompatible with the Mac architecture;
- any instruction would require disabling Gatekeeper or using Terminal to bypass it.

A stop is a valid test result.

## Evidence to return

Return:

1. machine/macOS/browser data;
2. screenshot and exact text of the first Gatekeeper alert;
3. screenshot and exact text of the Privacy & Security exception entry;
4. exact label clicked for **Open Anyway / Dennoch öffnen**;
5. screenshot and exact text of the second confirmation;
6. whether authentication was requested;
7. whether the local control center opened;
8. whether first-run Settings appeared;
9. whether app removal succeeded and `~/Notanda` remained;
10. any point where help was needed.

Do not include credentials or an OpenAlex key in screenshots.
