# Änderungen

Alle nennenswerten Änderungen an diesem Projekt. Versionen nach [Semantic Versioning](https://semver.org/lang/de/).

## [0.29.1] – 2026-11-11

### Neu
- **Automatische Sicherung vor jedem Datenbank-Update:** Bringt ein Update eine neue Datenbank-Version mit, legt die
  App vor dem Umbau eine Sicherung an (`…_vor-update.db.gz` im Paketordner, die letzten drei bleiben; sichtbar und
  wiederherstellbar unter Sicherung & Updates). Schlägt die Sicherung fehl, wird das Update der Datenbank abgebrochen
  und nichts verändert.

## [0.29.0] – 2026-11-11

### Neu
- **Mailversand über Microsoft 365 (Microsoft Graph)** – ohne SMTP: Verwaltung → Mail → „Versand über“ →
  Microsoft 365. Anmeldung als Entra-App mit der Anwendungsberechtigung `Mail.Send`, wahlweise per **Client-Secret**
  oder **Zertifikat** (PEM; die Anmeldung wird ohne Zusatzbibliothek selbst signiert). Anhänge (PDF/Excel) wie
  gewohnt, optional Kopie in „Gesendete Elemente“. Secret und Zertifikat werden nicht mitgesichert.
- **Eigener Mailweg über config.env:** `ZE_MAIL_COMMAND` (Programm bekommt die fertige Mail auf stdin) oder
  `ZE_MAIL_SOCKET` (Unix-Socket, Antwort „OK“). Bewusst nur in der config.env der NAS einstellbar, nicht über die
  Weboberfläche – und damit updatesicher, ohne Programmdateien anzupassen.
- Automatische Berichte, Erinnerungen und „Passwort vergessen“ prüfen jetzt den gewählten Mailweg statt nur den
  SMTP-Server. Wunsch von @denberg92 (#6).

## [0.28.0] – 2026-11-11

### Neu
- **Abwesenheiten als Kalender-Abo** (Reiter Abwesenheiten → Kalender-Abo) – für Nextcloud, iPhone, Outlook oder
  Thunderbird, schreibgeschützt. Jeder, der den Abwesenheitskalender sehen darf, legt sich einen **persönlichen
  Link** an; er zeigt genau das, was die Person auch in der App sieht. Admins können zusätzlich **Links für eine
  Filiale** (oder alle) anlegen, z. B. für einen gemeinsamen Team-Kalender – wahlweise mit Art (Urlaub, Krank …) oder
  nur „abwesend“. Bei beiden wählbar: **offene Anträge** als „(beantragt)“. Zusammenhängende Tage werden zu einem
  Termin zusammengefasst, Wochenenden und freie Tage unterbrechen ihn nicht. Links sind geheim, nur als Hash
  gespeichert und jederzeit löschbar; verliert jemand das Recht, liefert sein Link nichts mehr.
- **CSV-Import: nur zwei Spalten mit Datum und Uhrzeit** – z. B. `03.11.2026 07:02;03.11.2026 15:30`, mit oder ohne
  Kopfzeile, auch mit Spaltennamen wie „Datum Kommen“/„Datum Gehen“ oder „Arbeitsbeginn“/„Arbeitsende“, kurzen
  Jahreszahlen und „Uhr“. Nachtdienste über Mitternacht werden richtig erkannt.
  Wünsche von @ralfmehner-collab (#5).

## [0.27.0] – 2026-11-10

### Neu
- **Abwesenheitskalender für einzelne Mitarbeiter freigeben:** Unter Verwaltung → Mitarbeiter → Bearbeiten → Rolle
  gibt es den Haken **„Darf den Abwesenheitskalender sehen“** – auch ohne Admin- oder Vorgesetzten-Rechte. Freigegebene
  Mitarbeiter sehen die Kollegen ihrer Filiale(n) (ohne Filiale alle), aber nur als neutrales **„abwesend“** – ohne Art
  (Krankheit bleibt privat) und ohne offene Anträge. Die eigenen Einträge sehen sie vollständig.
- **Arbeitstage der Firma** (Verwaltung → Einstellungen → Allgemein): Diese Wochentage sind im Abwesenheitskalender weiß,
  die übrigen grau – z. B. Dienstag bis Samstag. Standard bleibt Montag bis Freitag.
  Wünsche von @ralfmehner-collab (#5).

## [0.26.2] – 2026-11-09

### Sicherheit (aus einem externen Review)
- **Schlüssel für das DSM-Fenster nur noch für den Paketbenutzer lesbar (0600):** `app.cgi` läuft als Paketbenutzer
  (auf einer NAS mit DSM 7 geprüft). Andere lokale Benutzer der NAS können sich damit nicht mehr gegenüber 127.0.0.1
  als beliebiger DSM-Benutzer ausgeben.
- **CSRF-Schutz:** Der Header `X-Forwarded-Host` zählt nicht mehr als eigene Adresse – er ließe sich je nach Proxy vom
  Browser aus setzen. Es zählen nur der `Host`-Header und die eingetragene öffentliche Adresse. Wer die App hinter einem
  Reverse Proxy betreibt, der `Host` nicht durchreicht, trägt die öffentliche Adresse in der Verwaltung ein.
- **Sitzungen laufen nach 7 statt 30 Tagen ab** (änderbar über `ZE_SESSION_DAYS`).
- **Sicherungen enthalten keine Browser-Sitzungen mehr.** Nach dem Wiederherstellen bleiben angemeldete Benutzer
  trotzdem angemeldet.

## [0.26.1] – 2026-11-08

### Geändert
- **Kurzbefehl-Anleitung für iOS 27** anhand echter Screenshots (iOS 27.2) neu geschrieben: „Bearbeiten“ statt
  KI-Beschreibung, „Inhalte von URL abrufen“, Methode POST, Header über „neue Überschrift hinzufügen“ (Schlüssel/Text),
  Auslöser Ort/NFC direkt im Kurzbefehl über den Knopf „Automation“. Die bisherigen Schritte stehen aufklappbar
  unter „iOS 26 und älter“. Danke an @ralfmehner-collab (#4).

## [0.26.0] – 2026-11-08

### Neu
- **Abwesenheitskalender** (Reiter „Abwesenheiten“) für Admins und Vorgesetzte: Monatsübersicht Mitarbeiter × Tage mit
  Urlaub, Krank, Gleittag, Überstunden-Abbau, Bildungsurlaub, Berufsschule, Elternzeit, halben Tagen und **offenen
  Anträgen** (gestrichelt); Wochenenden, Feiertage und Teilzeit-freie Tage sind markiert, unten steht die Zahl der
  Abwesenden pro Tag. Filter nach Filiale und einzelnen Mitarbeitern. Vorgesetzte ohne Admin-Rechte sehen nur ihr Team.
- **Überschneidungs-Hinweis beim Genehmigen:** Unter „Anträge zur Freigabe“ steht bei jedem offenen Antrag, wer aus
  derselben Filiale (sonst demselben Team) im Zeitraum schon fehlt – z. B. „Gleichzeitig weg: 2 von 5“ – mit Link
  direkt in den Kalender.
- **Stempel-Link ohne Kurzbefehle:** Die neue Seite `/stempeln` stempelt per Symbol auf dem Home-Bildschirm oder per
  **NFC-Aufkleber mit Web-Adresse** (z. B. mit der App „NFC Tools“ beschrieben). Das Handy wird einmal über die
  Einrichtungsseite gekoppelt (Schlüssel im HttpOnly-Cookie); der Aufkleber selbst enthält keinen Schlüssel und
  funktioniert für alle eingerichteten Handys. Umschalt-Aufkleber ignorieren einen zweiten Scan innerhalb einer Minute.
  Wünsche von @ralfmehner-collab (#4).

### Geändert
- **Einrichtungsseite fürs iPhone überarbeitet:** Stempel-Link und NFC zuerst, die Kurzbefehl-Anleitung nennt jetzt die
  Begriffe der verschiedenen iOS-Versionen (z. B. „Sofort ausführen“ bzw. „Vor dem Ausführen fragen“ aus) und rät
  bei iOS-Versionen mit KI-Beschreibung zur manuellen Bearbeitung, damit der Schlüssel nur im Kurzbefehl steht.
- Wegezeit kann zusätzlich für den Stempel-Link (Home-Bildschirm) eingeschaltet werden.

## [0.25.0] – 2026-11-04

### Neu
- **Kiosk pro Tablet anpassbar** (Verwaltung → Kiosk & Filialen, direkt in der Tabelle):
  - **Darstellung:** große Kacheln (wie bisher), kleine Kacheln für größere Teams oder eine Liste mit „da“-Kennzeichen.
  - **Anwesende zuerst:** wer gerade eingestempelt ist, steht oben.
  - **Anzeigedauer der Bestätigung** („Hallo …“ / „Tschüss …“): 2 bis 15 Sekunden, Standard jetzt 4 statt 7 Sekunden.
    Ein Balken zeigt die Restzeit, Antippen schließt sofort – der Nächste kann direkt stempeln.
  Änderungen übernimmt das Tablet innerhalb von 30 Sekunden. Wunsch von @ralfmehner-collab (#3).

## [0.24.3] – 2026-11-03

### Sicherheit
- **DSM-Passwortprüfung ohne Proxy und ohne Weiterleitungen:** Die Anfrage an DSM nutzt jetzt einen eigenen
  HTTP-Client. Er ignoriert `http_proxy`/`https_proxy` aus der Umgebung – bisher wäre die Anmeldung samt Passwort
  über einen so eingetragenen Proxy gelaufen, auch für 127.0.0.1. Weiterleitungen (3xx) werden nicht mehr befolgt,
  sondern als „DSM nicht erreichbar“ gewertet. Update-Prüfung und Mailversand sind nicht betroffen.

## [0.24.2] – 2026-11-03

### Sicherheit
- **DSM-Passwortprüfung nur noch auf der NAS selbst:** Die „DSM-Adresse für die Passwortprüfung“ darf nur noch auf
  die NAS zeigen (`127.0.0.1`, `localhost` oder `::1`, Port frei wählbar). Bisher ließ sich dort ein beliebiger Server
  eintragen – ein Admin der Zeiterfassung hätte so die DSM-Passwörter der Kollegen umleiten können, und bei einer
  `http://`-Adresse zu einem anderen Gerät wären sie unverschlüsselt durchs Netz gegangen. Geprüft wird beim
  Speichern und vor jeder Anmeldung; ein abweichender alter Wert wird durch `http://127.0.0.1:5000` ersetzt.
- Die Ausnahme von der Zertifikatsprüfung (selbst signiertes DSM-Zertifikat) prüfte nur den Anfang der Adresse,
  sodass z. B. `https://localhost.example.org` mitgemeint war. Jetzt wird der echte Hostname verglichen.

## [0.24.1] – 2026-11-02

### Geändert
- **Kiosk schöner und mittig:** Logo, Firmenname, Tablet-Name und eine große Uhr stehen jetzt zentriert oben,
  die Namen darunter als größere Kacheln mit Initialen (grüner Punkt = gerade da) – auch auf breiten Bildschirmen
  mittig statt links oben. Bei PIN-Eingabe und Kommen/Gehen wird der Kopf automatisch kompakt.

## [0.24.0] – 2026-11-02

### Neu
- **Firmenname und -logo:** unter Verwaltung → Einstellungen → Allgemein einen Firmennamen eintragen und ein Logo
  hochladen (PNG oder JPEG, bis 1 MB). Beides erscheint in der Kopfzeile, auf der Anmeldeseite, auf dem
  Kiosk-Tablet und oben in den Monats- und Jahresberichten (PDF und Excel). Transparente PNGs bleiben transparent.
  Das Logo liegt in der Datenbank und ist damit in jeder Sicherung enthalten. Wunsch von @ralfmehner-collab (#2).

## [0.23.1] – 2026-11-02

### Behoben
- **Kiosk auf dem iPad:** Nach „Zum Home-Bildschirm“ (oder wenn Safari Website-Daten gelöscht hatte) meldete das
  Tablet „nicht freigeschaltet“. Ursache: Ein Home-Bildschirm-Symbol hat auf iOS einen eigenen, leeren Speicher, und
  der Schlüssel wurde aus dem Link entfernt. Jetzt bleibt der Schlüssel im Link (`#k=…`, wird nie an den Server
  geschickt) und das Symbol nimmt ihn mit. Danke an @ralfmehner-collab (#2).
  **Bitte das alte Home-Bildschirm-Symbol löschen** und den Link aus der Verwaltung neu öffnen und speichern.

## [0.23.0] – 2026-11-02

### Geändert
- **Verwaltung übersichtlicher:** Statt einer langen Seite gibt es oben Unterbereiche – Mitarbeiter, Anträge
  (mit Zähler), Abschluss, Kiosk & Filialen, Einstellungen, Mail, Sicherung & Updates, Protokoll. Der zuletzt
  geöffnete Bereich bleibt gemerkt; auf dem Handy lässt sich die Leiste wischen.
- **Mitarbeiterliste kompakt:** Name, Arbeitszeit, Vorgesetzter und Kontostände auf einen Blick. „Bearbeiten“ klappt
  ein Formular in den Gruppen Zugang, Rolle und Konten auf (dort auch Deaktivieren und Löschen), „Arbeitszeit“ öffnet
  Stunden und Pausen je Wochentag. „Neuen Mitarbeiter anlegen“ ist eingeklappt.
- **Einstellungen in Gruppen** (Allgemein, Pausen & Arbeitszeitgesetz, Wegezeit, Vergessenes Ausstempeln,
  Anmeldung mit DSM) mit einem Speichern-Knopf unten; die Feiertagsliste ist zum Aufklappen.

## [0.22.0] – 2026-11-01

### Neu
- **Vier Designs zur Auswahl:**
  - *Klassisch* – wie bisher, folgt hell/dunkel des Geräts
  - *Synology DSM* – blau und kantiger, passt ins DSM-Fenster
  - *Nacht* – immer dunkel mit Bernstein-Akzent, angenehm im Nachtdienst
  - *Warm & groß* – Sandtöne, kräftiger Kontrast, größere Schrift und Bedienelemente
- Jeder wählt sein Design unter **Mein Zugang → Design** (gilt auf allen Geräten). Der Admin legt unter
  Verwaltung → Einstellungen das **Standard-Design** fest – für alle ohne eigene Wahl und für die Kiosk-Tablets.

## [0.21.0] – 2026-11-01

### Neu
- **Berufsschule** als Abwesenheit im Jahreskalender und in der Monatsansicht: Der Tag gilt als erfüllt
  (kein Minus), es wird kein Urlaub verbraucht.
- **Elternzeit** als Abwesenheit: kein Soll, kein Urlaubsverbrauch, wird in Berichten als eigene Zeile gezählt.
  Danke an @ralfmehner-collab (#1).

## [0.20.0] – 2026-11-01

### Neu
- **Urlaubsanträge mit Freigabe:** Unter Übersicht → „Urlaub beantragen“ stellen Mitarbeiter Anträge für Urlaub,
  Gleittag, Überstunden-Abbau, Bildungs- oder Sonderurlaub – auch für einen halben Tag. Der Vorgesetzte bekommt
  eine Mail und entscheidet unter „Anträge zur Freigabe“ (mit Resturlaub und Zähler am Reiter); erst nach dem
  Genehmigen werden die Tage eingetragen. Ablehnen geht mit Begründung, offene Anträge lassen sich zurückziehen.
- **Vorgesetzte:** In der Mitarbeiterliste lässt sich jedem ein Vorgesetzter zuordnen, z. B. der Filialleiter.
  Vorgesetzte brauchen keine Admin-Rechte und sehen nur die Anträge ihrer Mitarbeiter. Ohne Vorgesetzten gehen
  Anträge an die Admins.
- **Admins in der Oberfläche anlegen:** „Admin“ beim Anlegen ankreuzen oder in der Mitarbeiterliste umschalten.

### Behoben
- Mail zum Monatsbericht: „im Anhang findest du **deinen** Arbeitszeitnachweis“ (statt „deine“).

Danke an @ralfmehner-collab für alle drei Hinweise (#1).

## [0.19.0] – 2026-10-31

### Neu
- **Filialen fürs Kiosk-Tablet:** Unter Verwaltung → Kiosk-Tablets Filialen anlegen, Mitarbeiter zuordnen (auch
  mehreren) und jedem Tablet eine Filiale geben. Das Tablet zeigt dann nur die Mitarbeiter seiner Filiale –
  plus alle ohne Filiale, damit ohne Einrichtung alles wie bisher bleibt. Stempeln an einem fremden Tablet
  wird abgelehnt.
- **Kontostände am Kiosk:** Nach der PIN und nach dem Stempeln sieht jeder heute gearbeitet (mit Soll und
  Tagessaldo), sein Gleitzeit-Konto, das Überstunden-Konto (wenn er Ü-Tage oder einen Ü-Übertrag hat) und den
  Resturlaub. Danke an @ralfmehner-collab für beide Wünsche (#1).

## [0.18.1] – 2026-10-31

### Behoben
- **Fronleichnam in Sachsen und Thüringen:** Der Feiertag wurde landesweit gerechnet, gilt dort aber nur in
  einzelnen Gemeinden (Sachsen: sorbisches Siedlungsgebiet im Landkreis Bautzen; Thüringen: Eichsfeld und einzelne
  Gemeinden). Er ist jetzt standardmäßig aus. Danke an @ralfmehner-collab für den Hinweis (#1).

### Neu
- **Regionale Feiertage zum Ankreuzen** unter Verwaltung → Einstellungen, direkt unter dem Bundesland:
  Fronleichnam (Sachsen, Thüringen), Mariä Himmelfahrt (Bayern, voreingestellt an – gilt in den meisten
  Gemeinden) und das Augsburger Friedensfest (nur Stadt Augsburg).

## [0.18.0] – 2026-10-30

### Neu
- **Stunden je Wochentag:** Unter Verwaltung → Mitarbeiter → „Verlauf“ lässt sich statt „Wochenstunden
  gleichmäßig verteilen“ für jeden Tag eine eigene Sollzeit eintragen, z. B. 38 h als Di–Fr 8,5 h und Sa 4 h.
  Wie bisher mit „gilt ab“-Datum, ältere Monate bleiben unverändert.
- **Pausenabzug je Wochentag** pro Mitarbeiter: „Standard“ (Einstellung für alle), „keine“, „ArbZG“ oder feste
  Minuten – z. B. samstags kein Abzug. Steht auch im Kopf von PDF und Excel.
- **Mitarbeiter deaktivieren und wieder aktivieren** direkt in der Mitarbeiterliste; deaktivierte erscheinen
  grau am Ende der Liste.
- **Mitarbeiter endgültig löschen** – mit allen Buchungen, Abwesenheiten, Anträgen und Abschlüssen. Zur
  Sicherheit muss der Benutzername eingetippt werden, vorher entsteht automatisch eine Datensicherung
  („vor Löschen eines Mitarbeiters“). Im Änderungsprotokoll bleibt der Name als „(gelöscht)“ erhalten.
  Sich selbst und den letzten Admin kann man nicht löschen.

## [0.17.0] – 2026-10-23

### Neu
- **Sicherungen löschen** unter Verwaltung → Datensicherung: einzeln per Mülleimer oder mehrere per Häkchen
  („alle auswählen“ inklusive). Gelöscht wird im Paketordner und im freigegebenen Ordner. Die neueste Sicherung
  ist geschützt, damit nie alle weg sind. Jede Löschung steht im Änderungsprotokoll.

### Behoben
- Mehrere Sicherungen in derselben Minute werden jetzt richtig nach Zeit sortiert.

## [0.16.3] – 2026-10-23

### Sicherheit
- **Das Mail-Passwort (SMTP) wird nicht mehr mitgesichert.** Bisher stand es im Klartext in jeder Sicherung.
  Beim Wiederherstellen auf derselben NAS bleibt das aktuelle Passwort erhalten; fehlt es (z. B. nach dem
  Umzug auf eine neue NAS), weist die App darauf hin, es unter Verwaltung → Mailserver neu einzutragen.
  Ältere Sicherungen enthalten es noch – sie werden nach der eingestellten Aufbewahrung automatisch gelöscht.

## [0.16.2] – 2026-10-23

### Sicherheit
Härtung nach einem externen Sicherheitsreview:
- **Schutz vor CSRF:** Ändernde Anfragen mit Login-Cookie werden nur noch von der eigenen Seite angenommen.
  Eine fremde Webseite konnte bisher einen angemeldeten Nutzer z. B. ein- oder ausstempeln lassen.
- **DSM-Fenster:** Der interne Proxy-Schlüssel gilt nur noch für Verbindungen von der NAS selbst
  (127.0.0.1, ohne `X-Forwarded-For`).
- **Kurzbefehl-/NFC-Schlüssel gelten nur noch zum Stempeln.** Bisher hatten sie alle Rechte des Benutzers –
  beim Admin also auch die Verwaltung. Kurzbefehle, NFC und die Einrichtungsseite funktionieren unverändert.
- **CSV- und Excel-Exporte:** Freitext, der mit `=`, `+`, `-` oder `@` beginnt, wird entschärft, damit Excel
  daraus keine Formel macht.
- **Login-Sperre:** Die Liste der Fehlversuche wird aufgeräumt und ist in der Größe begrenzt.

### Neu
- Gelber **Warnhinweis**, wenn die App per unverschlüsseltem `http://` von außerhalb des Heimnetzes
  aufgerufen wird (Port direkt im Router freigegeben statt Reverse Proxy mit HTTPS).

### Dokumentation
- README: Hinweise zum Verschlüsseln der Datensicherung (Hyper Backup, verschlüsselter Ordner) und
  zum Reverse Proxy. SECURITY.md ergänzt.

## [0.16.1] – 2026-10-22

### Behoben
- Monatsansicht: Mit Hinweisen nach Arbeitszeitgesetz wurden die vier Kennzahlen oben zusammengedrückt
  (Zahlen brachen um). Der Hinweis steht jetzt in einer eigenen Zeile darunter.

### Dokumentation
- Neue Screenshots in der README: Kiosk-Tablet, Korrekturanträge, Monatsabschluss, Änderungsprotokoll,
  dazu aktualisierte Übersicht, Monat, Jahr, PDF und iPhone-Einrichtung (alle Daten erfunden).

## [0.16.0] – 2026-10-21

### Neu
- **Kiosk-Tablet:** eigene Seite `/kiosk` für ein Tablet an der Wache – Namenskacheln mit grünem Punkt
  für alle, die gerade da sind; Name antippen, PIN eingeben, „Kommen“ bzw. „Gehen“, kurze Bestätigung.
- Tablets schaltet ein Admin unter Verwaltung → Kiosk-Tablets frei (QR-Code/Link, jederzeit sperrbar).
- Jeder setzt seine 4–6-stellige **PIN** unter „Mein Zugang“; ein Admin kann sie zurücksetzen.
  Nur wer eine PIN hat, erscheint am Tablet. Nach 5 falschen PINs ist der Name 15 Minuten gesperrt.
- Wegezeit lässt sich auch für das Kiosk-Tablet einschalten.

## [0.15.0] – 2026-10-21

### Neu
- **Änderungsprotokoll in der Verwaltung:** wer hat wann was geändert – filterbar nach Mitarbeiter
  (betroffen oder handelnd), Zeitraum, Bereich (Buchungen, Anträge, Monatsabschluss, Mitarbeiter,
  Einstellungen, Berichte, Anmeldungen) und Suchtext (auch deutsches Datum wie „14.10.2026“).
  Klartext statt Kürzeln, Zeiten in Ortszeit, Nachladen älterer Einträge, CSV-Export.
- Jeder Protokolleintrag kennt den betroffenen Mitarbeiter – auch bei gelöschten Buchungen;
  vorhandene Einträge werden beim Update nachgetragen (Schema v10).

## [0.14.0] – 2026-10-21

### Neu
- **ARM-Modelle:** Ein Paket für alle NAS mit DSM 7 – Intel/AMD (x86_64), ARMv8 (z. B. DS223, DS124,
  DS220j) und ARMv7. Das Startskript wählt die passenden Bibliotheken für Prozessor und Python-Version.
  Getestet im Emulator für ARMv8 (Python 3.8 und 3.9) und ARMv7 (Python 3.9).

### Geändert
- Release-Datei heißt jetzt `zeiterfassung-<version>-noarch.spk` (vorher `…-x86_64.spk`). Für die
  Update-Funktion älterer Versionen liegt dieselbe Datei zusätzlich unter dem alten Namen im Release.
- Das Paket ist größer (rund 45 MB), weil es die Bibliotheken für alle Prozessoren enthält.

## [0.13.1] – 2026-10-21

### Sicherheit
- Die echte Absenderadresse hinter dem Reverse Proxy (`X-Forwarded-For`/`-Proto`) wird nur noch
  ausgewertet, wenn die Anfrage von der NAS selbst kommt (127.0.0.1/::1); maßgeblich ist die Adresse,
  die der Proxy anhängt. Vorher ließ sich eine Heimnetz-Adresse vortäuschen – damit kam man an die
  Paketquelle und konnte die Login-Sperre umgehen.
- Login-Sperre zusätzlich pro Benutzername: nach 10 Fehlversuchen in 15 Minuten, egal von welcher
  Adresse (bisher nur 5 pro Name und Adresse).

## [0.13.0] – 2026-10-21

### Neu
- **Korrekturanträge:** Mitarbeiter tippen in der Monatsansicht auf einen Tag und beantragen mit
  Begründung einen Nachtrag, eine Änderung von Kommen/Gehen oder das Löschen einer Buchung.
- Admins bekommen eine Mail und sehen offene Anträge unter Verwaltung → Korrekturanträge (Zähler am Reiter).
  **Genehmigen** übernimmt die Änderung direkt, **Ablehnen** mit Begründung; der Mitarbeiter bekommt die
  Entscheidung per Mail. Alles steht im Änderungsprotokoll.
- „Mein Zugang“ zeigt die eigenen Anträge mit Status; offene lassen sich zurückziehen.
- Schutz: keine Anträge in abgeschlossene Monate oder die Zukunft, Überschneidungen werden geprüft,
  wurde die Buchung zwischenzeitlich geändert, lässt sich der Antrag nicht mehr genehmigen.

## [0.12.0] – 2026-10-21

### Neu
- **Monatsabschluss pro Mitarbeiter:** Ein abgeschlossener Monat ist gesperrt (Buchungen, Abwesenheiten,
  Ü-Tage, Arbeitszeit-Verlauf, CSV-Import) und seine Kontostände am Monatsende sind festgeschrieben.
  Spätere Änderungen an Übertrag oder Wochenarbeitszeit wirken nur noch auf offene Monate.
- Abschließen in der Monatsansicht („🔒 Monat abschließen“) oder in der Verwaltung für alle Mitarbeiter
  bis zu einem Monat; abgeschlossen wird der Reihe nach. Wieder öffnen geht beim jeweils letzten
  abgeschlossenen Monat mit Begründung (steht im Änderungsprotokoll).
- PDF-Arbeitszeitnachweis zeigt „Monat abgeschlossen am …“

### Geändert
- Kontostände werden ab dem letzten Monatsabschluss gerechnet statt immer ab Erfassungsbeginn –
  das ist auch schneller, je länger die App im Einsatz ist.

## [0.11.0] – 2026-10-21

### Neu
- **Hinweise nach Arbeitszeitgesetz:** Tage mit mehr als 10 Stunden Arbeitszeit (§ 3) und weniger als
  11 Stunden Ruhezeit seit dem letzten Arbeitstag (§ 5) werden in Monatsansicht, PDF und Excel markiert;
  die Monatsübersicht zeigt die Anzahl. Ein zweiter Einsatz am selben Tag zählt nicht als Ruhezeit,
  Dienste über Mitternacht werden richtig erkannt. Abschaltbar unter Verwaltung → Einstellungen.

### Behoben
- PDF: Pfeile und Sonderzeichen (z. B. „6 h → 30 min“) wurden als „?“ gedruckt

## [0.10.0] – 2026-10-21

### Neu
- **Vergessenes Ausstempeln:** Läuft eine Buchung länger als 12 Stunden (einstellbar, 0 = aus), bekommen
  der Mitarbeiter und alle Admins mit Mailadresse eine Erinnerung. Ist sie um Mitternacht noch offen,
  endet sie automatisch zur Grenze (z. B. 06:50 + 12 h → 18:50).
- Automatisch beendete Tage sind in Monatsansicht, PDF und Excel mit „⚠ bitte prüfen“ markiert,
  bis ein Admin die Buchung korrigiert; alles steht im Änderungsprotokoll.

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

[0.29.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.29.1
[0.29.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.29.0
[0.28.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.28.0
[0.27.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.27.0
[0.26.2]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.26.2
[0.26.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.26.1
[0.26.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.26.0
[0.25.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.25.0
[0.24.3]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.24.3
[0.24.2]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.24.2
[0.24.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.24.1
[0.24.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.24.0
[0.23.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.23.1
[0.23.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.23.0
[0.22.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.22.0
[0.21.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.21.0
[0.20.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.20.0
[0.19.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.19.0
[0.18.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.18.1
[0.18.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.18.0
[0.17.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.17.0
[0.16.3]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.16.3
[0.16.2]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.16.2
[0.16.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.16.1
[0.16.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.16.0
[0.15.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.15.0
[0.14.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.14.0
[0.13.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.13.1
[0.13.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.13.0
[0.12.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.12.0
[0.11.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.11.0
[0.10.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.10.0
[0.9.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.9.0
[0.8.2]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.8.2
[0.8.1]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.8.1
[0.8.0]: https://github.com/rinkelzz/zeiterfassung/releases/tag/v0.8.0
