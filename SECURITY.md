# Sicherheit

Bitte Sicherheitslücken **nicht** als öffentliches Issue melden, sondern über
[GitHub → Security → Report a vulnerability](https://github.com/rinkelzz/zeiterfassung/security/advisories/new).
Ich melde mich so schnell wie möglich.

Unterstützt wird jeweils die neueste Version unter [Releases](https://github.com/rinkelzz/zeiterfassung/releases).

## Hinweise für den Betrieb

- Die App nur per **HTTPS** (Reverse Proxy mit Zertifikat) nach außen freigeben, nie Port 8765 direkt.
- Die QR-Code-Links enthalten persönliche Schlüssel. Verlorene Geräte unter „Mein Zugang“ bzw. in der
  Verwaltung sperren.
- Das SMTP-Passwort liegt in der Datenbank der App (`/var/packages/zeiterfassung/var/`), lesbar nur für
  den Paketbenutzer. Am besten ein eigenes Mail-Konto oder App-Passwort nur für den Versand verwenden.

# Security (English)

Please report vulnerabilities privately via GitHub's *Report a vulnerability* button, not as a public issue.
