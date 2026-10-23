# Sicherheit

Bitte Sicherheitslücken **nicht** als öffentliches Issue melden, sondern über
[GitHub → Security → Report a vulnerability](https://github.com/rinkelzz/zeiterfassung/security/advisories/new).
Ich melde mich so schnell wie möglich.

Unterstützt wird jeweils die neueste Version unter [Releases](https://github.com/rinkelzz/zeiterfassung/releases).

## Hinweise für den Betrieb

- Die App nur per **HTTPS** (Reverse Proxy mit Zertifikat) nach außen freigeben, nie Port 8765 direkt.
- Der Reverse Proxy sollte der von DSM auf derselben NAS sein: Nur Anfragen von 127.0.0.1 dürfen die
  echte Absenderadresse (`X-Forwarded-For`) mitliefern. Bei einem Proxy auf einem anderen Gerät sieht die
  App dessen Adresse – die Login-Sperre greift dann nur noch pro Benutzername.
- Die QR-Code-Links enthalten persönliche Schlüssel. Sie gelten nur zum Stempeln (`/api/v1/clock/…`),
  trotzdem verlorene Geräte unter „Mein Zugang“ bzw. in der Verwaltung sperren.
- Änderungen per Login-Cookie werden nur von der eigenen Seite angenommen (Schutz vor CSRF über
  `Sec-Fetch-Site` bzw. `Origin`). Läuft die App hinter einem Proxy, der den `Host`-Header umschreibt, die
  **Öffentliche Adresse** in den Einstellungen eintragen.
- Der Schlüssel für das DSM-Fenster (`.proxy_secret` im Paketordner) wird nur von Verbindungen über
  127.0.0.1 ohne `X-Forwarded-For` angenommen, also nur von `app.cgi` auf der NAS selbst.
- Updates kommen aus den GitHub-Releases dieses Repos. Sie sind nicht zusätzlich signiert – geschützt
  sind sie durch HTTPS und das (per Zwei-Faktor gesicherte) GitHub-Konto des Maintainers.
- Das SMTP-Passwort liegt in der Datenbank der App (`/var/packages/zeiterfassung/var/`), lesbar nur für
  den Paketbenutzer. Am besten ein eigenes Mail-Konto oder App-Passwort nur für den Versand verwenden.

# Security (English)

Please report vulnerabilities privately via GitHub's *Report a vulnerability* button, not as a public issue.
