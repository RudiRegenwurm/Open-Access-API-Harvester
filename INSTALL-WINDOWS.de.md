# Notanda 1.3.0 unter Windows installieren

**Stand:** Installer Preview 1, 21.09.2026  
**Status:** Arbeitsfassung für den veröffentlichten, noch unsignierten Installer. Die technische
Installation ist belegt. Der genaue SmartScreen-Wortlaut und die genaue GUI-Reihenfolge werden
erst nach dem laufenden Fremdrechner-Test als empirisch bestätigt markiert.

## Was Du brauchst

- einen 64-Bit-Windows-PC;
- Internetzugang;
- Deinen eigenen OpenAlex-API-Schlüssel für die erste Nutzung;
- **kein Python und kein Terminal**.

Der aktuelle Windows-Installer ist noch nicht digital signiert. Deshalb kann Windows beim ersten
Start eine SmartScreen-Warnung anzeigen. Schalte SmartScreen dafür **nicht** aus.

## 1. Installer herunterladen

Öffne die offizielle technische Vorabversion:

https://github.com/RudiRegenwurm/Open-Access-API-Harvester/releases/tag/v1.3.0-installer-preview.1

Lade unter **Assets** die Datei **Notanda-1.3.0.msi** herunter.

Der veröffentlichte SHA-256-Wert lautet:

`119ad7aadad955be20fe70903c29ebd774c6d1470e7a793746a3dd5d2ebe60ef`

Die Prüfsumme ist für die Nachvollziehbarkeit veröffentlicht; für den normalen Installationsweg
musst Du kein Terminal öffnen.

## 2. Installer starten

Öffne den Ordner **Downloads** und doppelklicke auf **Notanda-1.3.0.msi**.

### Wenn Windows SmartScreen warnt

Der Installer ist derzeit absichtlich noch unsigniert. Microsoft dokumentiert für diesen Zustand
den Weg über **Weitere Informationen / More info** und anschließend
**Trotzdem ausführen / Run anyway**.

**Wichtig:** Die exakte deutsche/englische Warnmeldung und die genaue Reihenfolge der sichtbaren
Elemente werden in dieser Anleitung erst nach dem aktuellen Fremdrechner-Test festgeschrieben.
Wenn Dein Windows **keine** Möglichkeit zum „Trotzdem ausführen / Run anyway“ anbietet, ändere
keine Sicherheitseinstellungen. Brich ab und melde den angezeigten Text.

## 3. Installation abschließen

Folge dem Notanda-Installationsdialog mit den normalen Standardoptionen.

**Empirischer Prüfpunkt:** Die genaue Folge der Installerfenster und Schaltflächen wird mit dem
Fremdrechner-Test ergänzt; hier wird nichts aus dem CI-Silent-Install-Test erfunden.

Nach Abschluss sollte Notanda als installierte Anwendung/Verknüpfung verfügbar sein.

## 4. Notanda starten

Starte **Notanda** über die installierte Verknüpfung beziehungsweise das Startmenü.

Notanda startet lokal auf Deinem Rechner und öffnet die Bedienoberfläche im Browser. Falls der
Browser nicht automatisch aufgeht, öffne:

`http://127.0.0.1:8765/`

Kein Terminal muss offen bleiben.

## 5. OpenAlex-Schlüssel beim ersten Start eintragen

Wenn noch kein OpenAlex-Schlüssel eingerichtet ist, führt Notanda beim ersten Start direkt in die
Einstellungen.

1. Besorge Deinen eigenen Schlüssel über `openalex.org/settings/api`.
2. Kopiere den Schlüssel.
3. Füge ihn in Notanda unter **Settings** beim OpenAlex-API-Key ein.
4. Speichere die Einstellung.

Du musst dafür keine Konfigurationsdatei suchen oder bearbeiten. Der Schlüssel wird lokal
gespeichert. Zeige ihn nicht in Screenshots oder Protokollen.

## 6. Kurze Funktionsprüfung

Nach dem Speichern des Schlüssels:

1. öffne **New Harvest**;
2. wähle **Conventional Search**;
3. gib eine kleine reale Suchanfrage ein;
4. lasse zunächst eine Vorschau anzeigen;
5. starte erst danach den Harvest.

Die Anwendung speichert ihre benutzereigenen Daten unter:

`%USERPROFILE%\Notanda`

Dort liegen insbesondere Konfiguration, Statusdatenbank, Reports und Corpus. Der Programmordner
und die Nutzerdaten sind getrennt.

## 7. Beenden und später wieder starten

Schließe Notanda in der normalen Desktop-Weise. Beim nächsten Start verwendest Du wieder die
installierte Notanda-Verknüpfung. Python, virtuelle Umgebungen und `pip` gehören nicht mehr zum
normalen Benutzerweg.

## 8. Deinstallieren

Öffne **Windows-Einstellungen → Apps**, suche **Notanda** und deinstalliere die Anwendung.

Die Deinstallation soll den benutzereigenen Ordner
`%USERPROFILE%\Notanda` mit Corpus und Provenienz nicht automatisch löschen.

## Wenn etwas nicht wie beschrieben aussieht

Nicht improvisieren und keine Windows-Sicherheitsfunktion deaktivieren. Notiere:

- Windows-Version und Sprache;
- die Stelle in dieser Anleitung;
- den genauen Wortlaut der Meldung;
- die sichtbaren Schaltflächen;
- nach Möglichkeit einen Screenshot ohne API-Schlüssel oder andere Zugangsdaten.

Für den begleiteten Beta-Zugang bleibt der Kontakt: **beta@notanda.io**.
