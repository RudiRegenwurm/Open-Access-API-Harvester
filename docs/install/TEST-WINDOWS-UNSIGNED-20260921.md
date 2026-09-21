# Notanda Installer Preview — Windows foreign-machine warning test

**Status:** ready for external execution  
**Date:** 2026-09-21  
**Artifact:** `Notanda-1.3.0.msi`  
**SHA-256:** `119ad7aadad955be20fe70903c29ebd774c6d1470e7a793746a3dd5d2ebe60ef`  
**Release:** https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1

## Purpose

This test records the real first-run experience of the current **unsigned** Windows installer on
a computer that is not a Notanda development machine. The goal is not merely to prove that the
installer works; it is to capture the exact security-warning text and click sequence a
non-technical user sees.

Do not change Windows security settings for this test. In particular, do not disable SmartScreen,
reputation-based protection, Smart App Control, antivirus, or the firewall.

## Before starting

Record:

- Windows edition, version and OS build;
- Windows display language;
- whether the account is a normal user or administrator;
- browser name and version used for the download;
- whether Notanda has ever been installed on this computer before.

Use the computer's normal browser and normal security configuration. Do not run PowerShell or a
terminal.

## Test sequence

1. Open the public Installer Preview release page:
   https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1
2. Download **Notanda-1.3.0.msi**.
3. Record any browser/download warning before changing or dismissing it:
   - screenshot;
   - exact wording;
   - buttons/links shown and their exact labels.
4. Open the downloaded MSI in the normal way (double-click from Downloads).
5. At the first Windows security warning:
   - **do not click yet**;
   - make a screenshot;
   - copy the exact title, body text, publisher/app information and all visible button/link labels
     into the test record.
6. If the dialog offers the normal SmartScreen disclosure control, select it and record the
   resulting second state before continuing. Microsoft currently documents the English path as
   **More info → Run anyway**; the test must record what this specific Windows machine actually
   shows.
7. Continue only through a visible **Run anyway / Trotzdem ausführen**-type option. If there is no
   bypass option, **stop the test**. Do not weaken security settings to manufacture one.
8. Record every installer screen in order:
   - screenshot;
   - window title;
   - explanatory text;
   - exact button pressed.
9. Complete the installation using normal defaults. Record whether Windows asks for elevation/UAC.
10. Start **Notanda** from the normal installed shortcut/start-menu entry. Do not use a terminal.
11. Confirm whether the browser opens automatically and whether
    `http://127.0.0.1:8765/` becomes reachable.
12. On a clean first start, confirm whether Notanda opens the Settings/setup path because no
    OpenAlex API key is configured. **Do not put an API key into the test record or screenshots.**
13. Close Notanda normally if the UI provides a normal route; otherwise close the application
    window/process in the normal desktop way.
14. Uninstall Notanda through Windows Settings → Apps. Record the uninstall sequence and whether
    `%USERPROFILE%\Notanda` remains afterward.

## Stop conditions

Stop and record the screen instead of improvising if:

- Windows offers no **Run anyway / Trotzdem ausführen** path;
- Smart App Control or organization policy states that the app cannot be run;
- the MSI requests an unexpected system change;
- the installer or app name differs from Notanda;
- the application exits before the local control center becomes reachable.

A stop is a valid test result, not a failed tester.

## Evidence to return

Return one compact record containing:

1. machine/OS/browser data from **Before starting**;
2. screenshots of every security-warning state;
3. the exact warning text and exact button labels;
4. installer screen/button sequence;
5. whether elevation was requested;
6. whether the browser/control center opened;
7. whether first-run Settings appeared;
8. whether uninstall succeeded and user data remained;
9. any point where help was needed.

Do not include an OpenAlex key, email credential or other secret in screenshots or notes.
