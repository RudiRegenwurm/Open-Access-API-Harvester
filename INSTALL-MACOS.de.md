# Notanda 1.3.0 unter macOS installieren

**Stand:** Installer Preview 1, 21.09.2026  
**Status:** Arbeitsfassung für den veröffentlichten, ad-hoc-signierten und nicht notarisierten
Installer. Der technische App-Start ist belegt. Der genaue Gatekeeper-Wortlaut wird erst nach dem
laufenden Fremdrechner-Test als empirisch bestätigt markiert.

## Was Du brauchst

- einen Mac, auf dem dieser Build lauffähig ist;
- Internetzugang;
- Deinen eigenen OpenAlex-API-Schlüssel für die erste Nutzung;
- **kein Python und kein Terminal**.

Notanda ist derzeit **nicht mit Apple Developer ID signiert und nicht notarisiert**. macOS wird
deshalb beim ersten Öffnen voraussichtlich eine Gatekeeper-Warnung zeigen.

## 1. Notanda herunterladen

Öffne die offizielle technische Vorabversion:

https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1

Lade unter **Assets** **Notanda-1.3.0.dmg** herunter.

Der veröffentlichte SHA-256-Wert lautet:

`928cbe2c1a031c00f5033cd75130d458f3a6c5a6122b5bd126ea549284fa2210`

Für den normalen Installationsweg ist kein Terminal erforderlich.

## 2. App in Programme kopieren

1. Öffne **Notanda-1.3.0.dmg**.
2. Kopiere **Notanda.app** mit dem Finder nach **Programme / Applications**.
3. Öffne danach **Programme**.
4. Doppelklicke **Notanda**.

## 3. Gatekeeper-Warnung einmalig freigeben

Beim ersten Öffnen kann macOS die App blockieren, weil sie nicht mit einer von Apple bestätigten
Developer-ID signiert und nicht notarisiert ist.

**Empirischer Prüfpunkt:** Der genaue erste Warntext wird erst nach dem aktuellen Fremdrechner-Test
festgeschrieben. Apples aktuelle Dokumentation zeigt mehrere mögliche Warnvarianten; wir setzen
keine davon ohne Beobachtung als Notanda-Wortlaut voraus.

Nach dem blockierten Öffnungsversuch gilt der in ADR 0005 festgelegte Weg:

1. Öffne **Systemeinstellungen**.
2. Öffne **Datenschutz & Sicherheit**.
3. Scrolle nach unten zum Bereich **Sicherheit**.
4. Suche dort den Hinweis zu Notanda.
5. Klicke **Dennoch öffnen**.
6. Bestätige Dich mit Touch ID oder Deinem Mac-Anmeldepasswort, falls macOS danach fragt.
7. Die Warnung erscheint erneut. Klicke **Öffnen**.

Danach speichert macOS Notanda als Ausnahme; spätere Starts sollten normal per Doppelklick möglich
sein.

Wenn **Dennoch öffnen** nicht angeboten wird, deaktiviere Gatekeeper nicht und benutze kein
Terminal-Kommando als Umgehung. Auf verwalteten Macs kann eine Organisationsrichtlinie die
Ausnahme verhindern.

## 4. Notanda startet lokal

Nach der Freigabe startet Notanda lokal und öffnet die Bedienoberfläche im Browser.

Falls der Browser nicht automatisch aufgeht, öffne:

`http://127.0.0.1:8765/`

Kein Terminal muss offen bleiben.

## 5. OpenAlex-Schlüssel beim ersten Start eintragen

Wenn noch kein OpenAlex-Schlüssel eingerichtet ist, führt Notanda beim ersten Start in die
Einstellungen.

1. Besorge Deinen eigenen Schlüssel über `openalex.org/settings/api`.
2. Kopiere ihn.
3. Füge ihn in Notanda unter **Settings** beim OpenAlex-API-Key ein.
4. Speichere die Einstellung.

Du musst keine Konfigurationsdatei bearbeiten. Der Schlüssel bleibt lokal und darf nicht in
Screenshots oder Protokollen auftauchen.

## 6. Kurze Funktionsprüfung

Nach dem Speichern:

1. öffne **New Harvest**;
2. wähle **Conventional Search**;
3. gib eine kleine reale Suchanfrage ein;
4. lasse zuerst die Vorschau anzeigen;
5. starte anschließend den Harvest.

Notanda legt benutzereigene Daten unter

`~/Notanda`

ab, einschließlich Konfiguration, Statusdatenbank, Reports und Corpus.

## 7. Beenden und deinstallieren

Beende Notanda wie eine normale Mac-App.

Zur Deinstallation entfernst Du **Notanda.app** aus **Programme**. Der benutzereigene Ordner
`~/Notanda` bleibt erhalten, damit Corpus und Provenienz nicht stillschweigend gelöscht werden.

## Wenn etwas nicht wie beschrieben aussieht

Ändere keine macOS-Sicherheitseinstellung über den beschriebenen **Dennoch öffnen**-Weg hinaus.
Notiere:

- Mac-Modell/Prozessor und macOS-Version;
- Sprache des Systems;
- die Stelle in dieser Anleitung;
- den genauen Warntext und alle sichtbaren Schaltflächen;
- nach Möglichkeit einen Screenshot ohne Zugangsdaten.

Für den begleiteten Beta-Zugang bleibt der Kontakt: **beta@notanda.io**.
