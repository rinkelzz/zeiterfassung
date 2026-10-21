# Änderungen

Alle nennenswerten Änderungen an diesem Projekt. Versionen nach [Semantic Versioning](https://semver.org/lang/de/).

## [0.9.0] – 2026-10-21

### Neu
- **Datensicherung:** jede Nacht (Uhrzeit wählbar) eine geprüfte, komprimierte Sicherung der Datenbank –
  immer im Paketordner (die letzten 7) und zusätzlich in einem freigegebenen Ordner
  (z. B. `/volume1/backup/zeiterfassung`, Aufbewahrung in Tagen einstellbar), den Hyper Backup mitsichern kann
- Verwaltung → **Datensicherung**: Ordner einrichten (mit Prüfung der Schreibrechte und Anleitung),
  „Sicherung jetzt erstellen“, Liste aller Sicherungen, Herunterladen, **Wiederherstellen**
  (der aktuelle Stand wird vorher automatisch gesichert)

## [0.8.2] – 2026-10-21

### Neu
- Versionsnummer und Link zu GitHub unten auf jeder Seite

### Geändert
- Paketquelle bietet ein neues Release sofort an, auch wenn die SPK noch auf die NAS geladen wird
- Paket nicht mehr als „Beta“ markiert

## [0.8.1] – 2026-10-20

### Neu
- **Updates über das Paket-Zentrum:** Die Zeiterfassung ist ihre eigene Paketquelle
  (`http://127.0.0.1:8765/spk`). Sie sieht regelmäßig bei GitHub nach neuen Releases, lädt die SPK
  auf die NAS und bietet sie dem Paket-Zentrum zum Aktualisieren an.
- Verwaltung → **Updates**: installierte und neueste Version, „Jetzt prüfen“, Anleitung zum Einrichten

## [0.8.0] – 2026-10-20

Erste öffentliche Version.

### Neu
- Berichte als **PDF** (Arbeitszeitnachweis pro Monat mit Kontoständen und Unterschriftszeilen,
  Jahresübersicht) und als **Excel** (Monat; Jahr mit einem Blatt pro Monat)
- **Berichte per Mail** über einen eigenen SMTP-Server: automatisch am 1. des Monats für den Vormonat
  und am 1. Januar für das Vorjahr, Nachholen bis zum 7., jeder Bericht nur einmal, Wiederholung bei Fehlern,
  Versandprotokoll; manueller Versand mit „✉ Mail“
- Mailadresse pro Mitarbeiter (Verwaltung oder selbst unter „Mein Zugang“), Testmail
- Download-Knöpfe PDF/Excel/CSV in Monats- und Jahresansicht

### Funktionen aus der Entwicklungsphase (0.1 – 0.7)
- Stempeln per Web, iPhone-Kurzbefehl, Standort-Automation, NFC (auch Umschalten mit einem Aufkleber), Kiosk
- Einrichtung fürs iPhone per QR-Code mit persönlicher Einrichtungsseite
- Wegezeit für gewählte Stempelwege
- Gleitzeit- und Überstunden-Konto, „Ü“-Tage, Wochenarbeitszeit mit „gilt ab“-Datum
- Urlaubskonto, Abwesenheiten (auch halbe Tage), Jahreskalender
- Feiertage aller Bundesländer, betriebsfreie Tage, Pausenabzug nach ArbZG oder fest
- CSV-Import (z. B. aus rinkelzz/gleitzeit) und CSV-Export, Änderungsprotokoll
- Synology-Paket für DSM 7: eigenes DSM-Fenster, Single Sign-on, Login mit DSM-Konto inkl. Zwei-Faktor

[0.9.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.9.0
[0.8.2]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.8.2
[0.8.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.8.1
[0.8.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.8.0
