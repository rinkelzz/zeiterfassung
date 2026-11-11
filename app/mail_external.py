"""Eigener Mailweg über ein Programm oder einen Unix-Socket – für Dienste wie einen eigenen Graph-Mailer.

Eingerichtet wird er nur in der config.env der NAS (bleibt bei Updates erhalten), nie über die Weboberfläche:
Ein Admin der Zeiterfassung soll keine Programme auf der NAS starten können.

  ZE_MAIL_COMMAND="/volume1/scripts/sendmail-graph.py --flag"
      Das Programm bekommt die fertige Mail (RFC 5322, inkl. Anhänge) auf stdin. Empfänger und Absender stehen
      in den Kopfzeilen To/From sowie in den Umgebungsvariablen ZE_MAIL_TO und ZE_MAIL_FROM.
      Exit-Code 0 = verschickt, sonst Fehler (stderr wird angezeigt). Zeitlimit 60 Sekunden.

  ZE_MAIL_SOCKET="/run/graphmailer/mail.sock"
      Die Mail wird an den Unix-Socket geschickt, danach wird die Schreibseite geschlossen. Der Dienst antwortet
      mit einer Zeile: „OK …“ (oder „250 …“) = verschickt, alles andere ist die Fehlermeldung.
"""
import os
import shlex
import socket
import subprocess

from .config import settings


class ExternalMailError(Exception):
    pass


def target() -> str:
    """Kurzbeschreibung für die Oberfläche, leer wenn nicht eingerichtet."""
    if settings.mail_command:
        args = shlex.split(settings.mail_command)
        return "Programm: " + args[0] if args else ""
    if settings.mail_socket:
        return "Unix-Socket: " + settings.mail_socket
    return ""


def send(raw: bytes, to: str, sender: str) -> None:
    if settings.mail_command:
        args = shlex.split(settings.mail_command)
        env = {**os.environ, "ZE_MAIL_TO": to, "ZE_MAIL_FROM": sender}
        try:
            r = subprocess.run(args, input=raw, capture_output=True, timeout=60, env=env)
        except FileNotFoundError:
            raise ExternalMailError(f"Mail-Programm nicht gefunden: {args[0]}")
        except subprocess.TimeoutExpired:
            raise ExternalMailError("Mail-Programm hat nicht innerhalb von 60 Sekunden geantwortet")
        except OSError as e:
            raise ExternalMailError(f"Mail-Programm konnte nicht gestartet werden: {e}")
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or b"").decode(errors="replace").strip().splitlines()
            raise ExternalMailError(f"Mail-Programm meldet Fehler ({r.returncode}): {msg[-1] if msg else 'ohne Meldung'}")
        return
    if settings.mail_socket:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(60)
                s.connect(settings.mail_socket)
                s.sendall(raw)
                s.shutdown(socket.SHUT_WR)
                reply = b""
                while len(reply) < 4096:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
        except (OSError, socket.timeout) as e:
            raise ExternalMailError(f"Mail-Dienst am Socket nicht erreichbar: {e}")
        line = reply.decode(errors="replace").strip().splitlines()[0] if reply.strip() else ""
        if not (line.upper().startswith("OK") or line.startswith("250")):
            raise ExternalMailError(f"Mail-Dienst meldet Fehler: {line or 'keine Antwort'}")
        return
    raise ExternalMailError("Kein eigener Mailweg in config.env eingetragen (ZE_MAIL_COMMAND oder ZE_MAIL_SOCKET)")
