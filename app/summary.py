"""Monatszusammenfassung aller Mitarbeiter – z. B. für das Steuerbüro / die Lohnbuchhaltung.

Eine Zeile je Mitarbeiter: Ist, Soll, Differenz, Kontostände, Urlaub (im Monat, Rest im Jahr),
Krank / Kind krank / andere Abwesenheiten – dazu die Abwesenheitszeiträume mit Datum.
Als PDF und Excel; dazu auf Wunsch alle Arbeitszeitnachweise des Monats in einem PDF.
Versand am 1. des Folgemonats je Verteiler (Tabelle summary_lists: Adressen + Auswahl alle / Filialen /
Mitarbeiter), Protokoll in summary_mail_log.
"""
import io
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from . import csvsafe
from .db import audit, get_setting
from .pdf import PDF, fit
from .report_files import (
    GREY, HEAD, LIGHT, L, R, MONTHS_DE, _dur, _hours, _logo_for, _num, _signed_color,
    build_data, month_pdf, parse_period,
)
from .reports import ABSENCE_KINDS, SICK_KINDS
from .timeutil import now_utc, now_utc_str, parse_utc, to_local, to_utc_str

MIME_PDF = "application/pdf"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# eigene Spalten im PDF, alles andere landet unter „Andere“
PDF_KINDS = ("urlaub", "krank", "kind_krank")


def summary_users(conn: sqlite3.Connection, d_from: date, d_to: date) -> List[sqlite3.Row]:
    """Aktive Mitarbeiter – und ausgeschiedene, die im Monat noch Zeiten oder Abwesenheiten haben."""
    rows = conn.execute(
        "SELECT * FROM users u WHERE (u.active = 1 OR EXISTS (SELECT 1 FROM entries e WHERE e.user_id = u.id "
        "AND e.start_utc < ? AND (e.end_utc IS NULL OR e.end_utc >= ?)) OR EXISTS (SELECT 1 FROM absences a "
        "WHERE a.user_id = u.id AND a.day BETWEEN ? AND ?)) ORDER BY u.display_name COLLATE NOCASE",
        ((d_to + timedelta(days=2)).isoformat(), (d_from - timedelta(days=1)).isoformat(),
         d_from.isoformat(), d_to.isoformat()),
    ).fetchall()
    out = []
    for u in rows:
        ts = u["tracking_start"]
        if ts and ts > d_to.isoformat():
            continue  # Erfassung beginnt erst nach dem Monat
        out.append(u)
    return out


def absence_periods(days: List[Dict]) -> List[Dict]:
    """Abwesenheiten zu Zeiträumen zusammenfassen: gleiche Art, dazwischen nur freie Tage (Wochenende, Feiertag)."""
    out: List[Dict] = []
    current: Optional[Dict] = None
    gap_ok = True
    for d in days:
        kind = d["absence"]
        if kind == "feiertag":
            kind = None  # gesetzliche Feiertage sind keine persönliche Abwesenheit
        if d["before_start"]:
            continue
        if kind:
            n = 0.5 if d["half_day"] else 1.0
            counted = d["workday"] and (kind == "feiertag" or not d["holiday"])
            if current and current["kind"] == kind and gap_ok and not d["half_day"] and not current["half_end"]:
                current["to"] = d["date"]
                current["days"] += n if counted else 0
            else:
                current = {"kind": kind, "label": ABSENCE_KINDS.get(kind, kind), "from": d["date"], "to": d["date"],
                           "days": n if counted else 0, "half": bool(d["half_day"]), "half_end": bool(d["half_day"])}
                out.append(current)
            gap_ok = True
        elif d["workday"] and not d["holiday"]:
            gap_ok = False  # ein normaler Arbeitstag unterbricht den Zeitraum
            current = None
    return out


def period_text(p: Dict) -> str:
    f, t = date.fromisoformat(p["from"]), date.fromisoformat(p["to"])
    when = f"{f:%d.%m.}" if f == t else f"{f:%d.%m.}–{t:%d.%m.}"
    n = p["days"]
    amount = "½ Tag" if n == 0.5 else f"{_num(n)} Tag{'e' if n != 1 else ''}"
    return f"{p['label']} {when} ({amount})"


def selection_user_ids(conn: sqlite3.Connection, branch_ids, user_ids) -> Set[int]:
    """Mitarbeiter der gewählten Filialen plus einzeln gewählte Mitarbeiter."""
    out = {int(u) for u in user_ids}
    b = [int(x) for x in branch_ids]
    if b:
        out |= {r["user_id"] for r in conn.execute(
            f"SELECT user_id FROM user_branches WHERE branch_id IN ({','.join('?' * len(b))})", b)}
    return out


def build_summary(conn: sqlite3.Connection, period: str, only: Optional[Set[int]] = None,
                  scope: str = "Alle Mitarbeiter") -> Dict:
    """only = Menge der Nutzer-IDs (None = alle); scope = Beschriftung im Kopf („Filiale Nord“)."""
    kind, d_from, d_to = parse_period(period)
    if kind != "month":
        raise ValueError("Die Zusammenfassung gibt es pro Monat (JJJJ-MM)")
    closed = {r["user_id"] for r in conn.execute("SELECT user_id FROM month_closings WHERE month = ?", (period,))}
    branches: Dict[int, List[str]] = {}
    for r in conn.execute("SELECT ub.user_id, b.name FROM user_branches ub JOIN branches b ON b.id = ub.branch_id "
                          "ORDER BY b.name"):
        branches.setdefault(r["user_id"], []).append(r["name"])
    rows, datas = [], []
    for u in summary_users(conn, d_from, d_to):
        if only is not None and u["id"] not in only:
            continue
        data = build_data(conn, u, period)
        datas.append(data)
        rep, t = data["report"], data["report"]["totals"]
        vac = data["vacation"]
        eb = data["end_balance"]
        rows.append({
            "user_id": u["id"],
            "name": u["display_name"],
            "username": u["username"],
            "personnel_no": u["personnel_no"] if "personnel_no" in u.keys() else None,
            "branches": ", ".join(branches.get(u["id"], [])),
            "active": bool(u["active"]),
            "worked_s": t["worked_s"], "target_s": t["target_s"], "diff_s": t["diff_s"],
            "gleitzeit_s": eb["gleitzeit_s"], "ueberstunden_s": eb["ueberstunden_s"],
            "absences": data["absences"],
            "vacation_month": sum(x["vacation_days"] for x in rep["days"]),
            "vacation": vac,
            "periods": absence_periods(rep["days"]),
            "closed": u["id"] in closed,
        })
    return {
        "period": period,
        "scope": scope,
        "title": f"{MONTHS_DE[d_from.month - 1]} {d_from.year}",
        "from": d_from,
        "to": d_to,
        "rows": rows,
        "reports": datas,
        "company": get_setting(conn, "company_name") or "",
        "logo": _logo_for(conn),
        "created": to_local(now_utc()),
    }


# --- PDF ---------------------------------------------------------------------------

def summary_pdf(s: Dict) -> bytes:
    from . import logo
    pdf = PDF(f"Monatszusammenfassung {s['title']}")

    def head(first: bool) -> float:
        if first:
            img, company, x = s.get("logo"), s.get("company"), L
            if img:
                w, h = logo.fit_box(img["width"], img["height"], 150, 30)
                pdf.image(L, 40 - h, w, h, img)
                x = L + w + 10
            if company:
                pdf.text(x, 36, fit(company, R - x - 10, 11, True), 11, True, color=(0.25, 0.25, 0.25))
            pdf.text(L, 58, "Monatszusammenfassung", 17, True)
            pdf.text(R, 58, s["title"], 13, True, align="right")
            pdf.text(L, 76, fit(f"{s['scope']} · {s['from']:%d.%m.%Y} – {s['to']:%d.%m.%Y}", 300, 9.5), 9.5)
            pdf.text(R, 76, "Stunden in h:mm · Abwesenheiten in Arbeitstagen", 8, align="right", color=GREY)
            pdf.line(L, 84, R, 84, 1, (0.3, 0.3, 0.3))
            return 98
        pdf.text(L, 815, f"Erstellt am {s['created']:%d.%m.%Y um %H:%M} Uhr · Zeiterfassung", 7, color=GREY)
        pdf.new_page()
        pdf.text(L, 46, f"Monatszusammenfassung {s['title']}", 11, True)
        pdf.text(R, 46, "Fortsetzung", 8, align="right", color=GREY)
        pdf.line(L, 54, R, 54, 0.8, (0.3, 0.3, 0.3))
        return 70

    cols = [("Mitarbeiter", 117, "left"), ("Ist", 40, "right"), ("Soll", 40, "right"), ("Differenz", 44, "right"),
            ("Gleitzeit", 42, "right"), ("Überstd.", 42, "right"), ("Urlaub", 34, "right"), ("Resturl.", 38, "right"),
            ("Krank", 32, "right"), ("Kind kr.", 36, "right"), ("Andere", 50, "right")]
    xs, x = [], L
    for _, w, _a in cols:
        xs.append(x)
        x += w

    def cx(i):
        return xs[i] + cols[i][1] - 4 if cols[i][2] == "right" else xs[i] + 3

    def table_head(y):
        pdf.rect(L, y, R - L, 24, HEAD)
        for i, (title, _, al) in enumerate(cols):
            pdf.text(cx(i), y + 10, title, 7.6, True, align=al)
        pdf.text(cx(4), y + 19.5, "Konto", 6.5, align="right", color=GREY)
        pdf.text(cx(5), y + 19.5, "Konto", 6.5, align="right", color=GREY)
        pdf.text(cx(6), y + 19.5, "im Monat", 6.5, align="right", color=GREY)
        pdf.text(cx(7), y + 19.5, "Jahr", 6.5, align="right", color=GREY)
        return y + 25

    def days(v):
        return _num(v) if v else ""

    y = table_head(head(True))
    rh = 22
    tot = {"worked_s": 0, "target_s": 0, "diff_s": 0, "vac": 0.0, "krank": 0.0, "kind_krank": 0.0, "other": 0.0}
    for i, r in enumerate(s["rows"]):
        if y + rh > 790:
            y = table_head(head(False))
        if i % 2:
            pdf.rect(L, y, R - L, rh, LIGHT)
        ab = r["absences"]
        other = sum(v for k, v in ab.items() if k not in PDF_KINDS and k != "feiertag")
        vac = r["vacation"]
        name = r["name"] + ("" if r["active"] else " (ausgeschieden)")
        pdf.text(cx(0), y + 9.5, fit(name, cols[0][1] - 6, 8.2, True), 8.2, True)
        sub = r["branches"] or r["username"]
        if not r["closed"]:
            sub = (sub + " · " if sub else "") + "nicht abgeschlossen"
        pdf.text(cx(0), y + 18.5, fit(sub, cols[0][1] - 6, 6.5), 6.5, color=GREY)
        vals = [None, _dur(r["worked_s"]), _dur(r["target_s"]), _dur(r["diff_s"], True),
                _dur(r["gleitzeit_s"], True), _dur(r["ueberstunden_s"], True),
                days(r["vacation_month"]), _num(vac["remaining"]) if vac else "–",
                days(ab.get("krank")), days(ab.get("kind_krank")), days(other)]
        for j, v in enumerate(vals):
            if v is None:
                continue
            color = _signed_color(r["diff_s"] if j == 3 else r["gleitzeit_s"] if j == 4 else
                                  r["ueberstunden_s"] if j == 5 else 0)
            pdf.text(cx(j), y + 13, v, 8.2, align=cols[j][2], color=color)
        y += rh
        pdf.line(L, y, R, y, 0.25, (0.85, 0.85, 0.85))
        tot["worked_s"] += r["worked_s"]
        tot["target_s"] += r["target_s"]
        tot["diff_s"] += r["diff_s"]
        tot["vac"] += r["vacation_month"]
        tot["krank"] += ab.get("krank", 0)
        tot["kind_krank"] += ab.get("kind_krank", 0)
        tot["other"] += other
    pdf.line(L, y, R, y, 0.8, (0.3, 0.3, 0.3))
    pdf.text(cx(0), y + 11, f"Summe ({len(s['rows'])} Mitarbeiter)", 8.2, True)
    for j, v in ((1, _dur(tot["worked_s"])), (2, _dur(tot["target_s"])), (3, _dur(tot["diff_s"], True)),
                 (6, days(tot["vac"])), (8, days(tot["krank"])), (9, days(tot["kind_krank"])), (10, days(tot["other"]))):
        pdf.text(cx(j), y + 11, v, 8.2, True, cols[j][2])
    y += 26
    pdf.text(L, y, "Gleitzeit / Überstunden: Kontostand am Monatsende (inkl. Übertrag). Resturlaub: für das ganze Jahr, "
                   "inkl. bereits geplanter Tage.", 7, color=GREY)
    y += 22

    # Abwesenheiten im Einzelnen
    listed = [r for r in s["rows"] if r["periods"]]
    if y > 740:
        y = head(False)
    pdf.text(L, y, "Abwesenheiten im Einzelnen", 10.5, True)
    y += 14
    if not listed:
        pdf.text(L, y, "Keine Abwesenheiten in diesem Monat.", 8.5, color=GREY)
    for r in listed:
        # Zeiträume auf mehrere Zeilen verteilen
        lines, cur = [], ""
        for p in r["periods"]:
            part = period_text(p)
            trial = (cur + "  ·  " + part) if cur else part
            if cur and pdf_width(trial) > R - L - 130:
                lines.append(cur)
                cur = part
            else:
                cur = trial
        if cur:
            lines.append(cur)
        need = 12 * len(lines) + 4
        if y + need > 795:
            y = head(False)
        pdf.text(L, y, fit(r["name"], 122, 8.2, True), 8.2, True)
        for n, line in enumerate(lines):
            pdf.text(L + 130, y + 12 * n, line, 8.2)
        y += need
        pdf.line(L, y - 8, R, y - 8, 0.25, (0.88, 0.88, 0.88))
    pdf.text(L, 815, f"Erstellt am {s['created']:%d.%m.%Y um %H:%M} Uhr · Zeiterfassung", 7, color=GREY)
    return pdf.output()


def pdf_width(text: str) -> float:
    from .pdf import text_width
    return text_width(text, 8.2)


def reports_pdf(s: Dict) -> bytes:
    """Alle Arbeitszeitnachweise des Monats in einem PDF (je Mitarbeiter ab einer neuen Seite)."""
    pdf = PDF(f"Arbeitszeitnachweise {s['title']}")
    for i, data in enumerate(s["reports"]):
        if i == 0:
            pdf.pages.clear()  # month_pdf beginnt jede Person mit einer neuen Seite
        month_pdf(data, pdf)
    if not s["reports"]:
        pdf.text(L, 60, f"Arbeitszeitnachweise {s['title']}: keine Mitarbeiter im Zeitraum.", 11)
    return pdf.output()


# --- Excel ---------------------------------------------------------------------------

def summary_xlsx(s: Dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="DCE3EF")
    hfmt = '+0.00;-0.00;0.00'
    used = [k for k in ABSENCE_KINDS if k != "feiertag" and any(r["absences"].get(k) for r in s["rows"])]
    for k in ("krank", "kind_krank"):
        if k not in used:
            used.append(k)  # Krank-Spalten immer zeigen – „0“ ist für die Lohnbuchhaltung auch eine Aussage

    wb = Workbook()
    ws = wb.active
    ws.title = "Übersicht"
    ws.append([f"Monatszusammenfassung {s['title']}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([s["company"], f"{s['from']:%d.%m.%Y} – {s['to']:%d.%m.%Y}"])
    csvsafe.no_formulas(ws)
    ws.append([])
    header = (["Mitarbeiter", "Benutzer", "Filiale", "Ist (h)", "Soll (h)", "Differenz (h)",
               "Gleitzeit-Konto (h)", "Überstunden-Konto (h)", "Urlaub im Monat (Tage)",
               "Urlaubsanspruch (Tage)", "Übertrag Vorjahr (Tage)", "Urlaub genommen (Tage)",
               "Urlaub geplant (Tage)", "Resturlaub (Tage)"]
              + [f"{ABSENCE_KINDS[k]} (Tage)" for k in used if k != "urlaub"]
              + ["Monat abgeschlossen", "Abwesenheiten"])
    ws.append(header)
    hr = ws.max_row
    for c in ws[hr]:
        c.font = bold
        c.fill = head_fill
        c.alignment = Alignment(wrap_text=True, vertical="center")
    first = hr + 1
    for r in s["rows"]:
        vac = r["vacation"] or {}
        ws.append([csvsafe.cell(r["name"] + ("" if r["active"] else " (ausgeschieden)")), csvsafe.cell(r["username"]),
                   csvsafe.cell(r["branches"]),
                   _hours(r["worked_s"]), _hours(r["target_s"]), _hours(r["diff_s"]),
                   _hours(r["gleitzeit_s"]), _hours(r["ueberstunden_s"]), r["vacation_month"],
                   vac.get("entitlement"), vac.get("carryover"), vac.get("taken"), vac.get("planned"),
                   vac.get("remaining")]
                  + [r["absences"].get(k, 0) for k in used if k != "urlaub"]
                  + ["ja" if r["closed"] else "nein",
                     csvsafe.cell(", ".join(period_text(p) for p in r["periods"]))])
        row = ws.max_row
        for col in (4, 5):
            ws.cell(row, col).number_format = "0.00"
        for col in (6, 7, 8):
            ws.cell(row, col).number_format = hfmt
    last = ws.max_row
    n_abs = len([k for k in used if k != "urlaub"])
    sum_cols = [4, 5, 6, 9] + list(range(15, 15 + n_abs))
    row = ["Summe", "", ""] + [""] * (len(header) - 3)
    for col in sum_cols:
        letter = _col(col)
        row[col - 1] = f"=SUM({letter}{first}:{letter}{last})" if last >= first else 0
    ws.append(row)
    sr = ws.max_row
    for c in ws[sr]:
        c.font = bold
    for col in (4, 5):
        ws.cell(sr, col).number_format = "0.00"
    ws.cell(sr, 6).number_format = hfmt
    widths = [24, 12, 16, 9, 9, 10, 11, 12, 10, 10, 10, 10, 10, 10] + [10] * n_abs + [11, 60]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[_col(i)].width = w
    ws.row_dimensions[hr].height = 45
    ws.freeze_panes = ws.cell(hr + 1, 2)

    # Blatt 2: Zeiträume einzeln – gut zum Filtern
    ws2 = wb.create_sheet("Abwesenheiten")
    ws2.append(["Mitarbeiter", "Art", "von", "bis", "Arbeitstage", "halber Tag"])
    for c in ws2[1]:
        c.font = bold
        c.fill = head_fill
    for r in s["rows"]:
        for p in r["periods"]:
            ws2.append([csvsafe.cell(r["name"]), p["label"], date.fromisoformat(p["from"]),
                        date.fromisoformat(p["to"]), p["days"], "ja" if p["half"] else ""])
            ws2.cell(ws2.max_row, 3).number_format = "DD.MM.YYYY"
            ws2.cell(ws2.max_row, 4).number_format = "DD.MM.YYYY"
    for col, w in zip("ABCDEF", (24, 20, 12, 12, 12, 10)):
        ws2.column_dimensions[col].width = w
    ws2.freeze_panes = "A2"
    ws2.auto_filter.ref = f"A1:F{max(ws2.max_row, 1)}"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _col(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


# --- Dateien und Versand ---------------------------------------------------------------

def files_for(s: Dict, with_reports: bool) -> List[Tuple[str, bytes, str]]:
    """[(Dateiname, Inhalt, MIME-Typ)] für Download und Mail."""
    p = s["period"]
    out = [(f"Monatszusammenfassung_{p}.pdf", summary_pdf(s), MIME_PDF),
           (f"Monatszusammenfassung_{p}.xlsx", summary_xlsx(s), MIME_XLSX)]
    if with_reports:
        out.append((f"Arbeitszeitnachweise_{p}.pdf", reports_pdf(s), MIME_PDF))
    return out


def split_addresses(raw: str) -> List[str]:
    return list(dict.fromkeys(a.strip() for a in (raw or "").replace(";", ",").split(",") if a.strip()))


def _ids(raw: str) -> List[int]:
    return [int(x) for x in (raw or "").split(",") if x.strip().isdigit()]


def list_out(conn: sqlite3.Connection, r: sqlite3.Row) -> Dict:
    return {
        "id": r["id"], "name": r["name"], "recipients": r["recipients"],
        "all_users": bool(r["all_users"]), "branch_ids": _ids(r["branch_ids"]), "user_ids": _ids(r["user_ids"]),
        "with_reports": bool(r["with_reports"]), "auto": bool(r["auto"]),
    }


def lists(conn: sqlite3.Connection) -> List[Dict]:
    return [list_out(conn, r) for r in conn.execute("SELECT * FROM summary_lists ORDER BY name COLLATE NOCASE, id")]


def scope_text(conn: sqlite3.Connection, lst: Dict) -> str:
    if lst["all_users"]:
        return "Alle Mitarbeiter"
    parts = []
    if lst["branch_ids"]:
        q = ",".join("?" * len(lst["branch_ids"]))
        names = [r["name"] for r in conn.execute(f"SELECT name FROM branches WHERE id IN ({q}) ORDER BY name",
                                                 lst["branch_ids"])]
        if names:
            parts.append(("Filiale " if len(names) == 1 else "Filialen ") + ", ".join(names))
    n = len(lst["user_ids"])
    if n:
        parts.append(f"{n} weitere{'r' if n == 1 else ''} Mitarbeiter" if parts else
                     f"{n} ausgewählte{'r' if n == 1 else ''} Mitarbeiter")
    return " + ".join(parts) or "keine Auswahl"


def build_for_list(conn: sqlite3.Connection, period: str, lst: Dict) -> Dict:
    only = None if lst["all_users"] else selection_user_ids(conn, lst["branch_ids"], lst["user_ids"])
    return build_summary(conn, period, only, scope_text(conn, lst))


def get_list(conn: sqlite3.Connection, list_id: int) -> Optional[Dict]:
    r = conn.execute("SELECT * FROM summary_lists WHERE id = ?", (list_id,)).fetchone()
    return list_out(conn, r) if r else None


def mail_text(s: Dict, with_reports: bool) -> Tuple[str, str]:
    company = f" – {s['company']}" if s["company"] else ""
    scope = "" if s["scope"] == "Alle Mitarbeiter" else f" ({s['scope']})"
    subject = f"Monatszusammenfassung {s['title']}{scope}{company}"
    open_ = [r["name"] for r in s["rows"] if not r["closed"]]
    lines = [
        "Guten Tag,",
        "",
        f"im Anhang finden Sie die Monatszusammenfassung für {s['title']}{scope} als PDF und Excel "
        f"({len(s['rows'])} Mitarbeiter: Arbeitsstunden, Kontostände, Urlaub, Krankheitstage und weitere Abwesenheiten).",
    ]
    if with_reports:
        lines.append("Zusätzlich liegen die Arbeitszeitnachweise dieser Mitarbeiter in einer PDF-Datei bei.")
    if open_:
        lines += ["", "Hinweis: Für folgende Mitarbeiter ist der Monat noch nicht abgeschlossen – "
                      "die Werte können sich noch ändern: " + ", ".join(open_) + "."]
    lines += ["", "Diese Mail wurde automatisch von der Zeiterfassung verschickt."]
    return subject, "\n".join(lines)


def send_list(conn: sqlite3.Connection, period: str, lst: Dict, actor_id: Optional[int] = None) -> List[str]:
    """Zusammenfassung für einen Verteiler erzeugen und an dessen Adressen schicken."""
    from .mailer import MailError, send_mail, valid_email
    to = split_addresses(lst["recipients"])
    if not to:
        raise MailError(f"Verteiler „{lst['name']}“ hat keine Adresse")
    bad = [a for a in to if not valid_email(a)]
    if bad:
        raise MailError(f"Ungültige Adresse: {', '.join(bad)}")
    s = build_for_list(conn, period, lst)
    if not s["rows"]:
        raise MailError(f"Verteiler „{lst['name']}“: keine Mitarbeiter in der Auswahl")
    subject, body = mail_text(s, lst["with_reports"])
    attachments = files_for(s, lst["with_reports"])
    for addr in to:  # einzeln – so sieht kein Empfänger die anderen Adressen
        send_mail(conn, addr, subject, body, attachments)
    audit(conn, actor_id, "summary_mailed", None, f"{period} {lst['name']} → {', '.join(to)}")
    return to


def _log(conn, period: str, list_id: int, ok: bool, to: str, error: str = "",
         when: Optional[datetime] = None) -> None:
    now = to_utc_str(when) if when else now_utc_str()
    conn.execute(
        "INSERT INTO summary_mail_log(period, list_id, status, attempts, last_attempt, sent_at, recipient, error) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?) ON CONFLICT(period, list_id) DO UPDATE SET "
        "status = excluded.status, attempts = summary_mail_log.attempts + 1, last_attempt = excluded.last_attempt, "
        "sent_at = COALESCE(excluded.sent_at, summary_mail_log.sent_at), recipient = excluded.recipient, "
        "error = excluded.error",
        (period, list_id, "sent" if ok else "error", now, now if ok else None, to, error[:500]),
    )


def run_due(conn: sqlite3.Connection, now: Optional[datetime] = None) -> List[Dict]:
    """Am 1. des Monats ab 6 Uhr (bis zum 7. nachholen): Zusammenfassung des Vormonats – je Verteiler einmal."""
    from .mailer import CATCH_UP_DAYS, MAX_ATTEMPTS, RETRY_AFTER, SEND_HOUR, mail_ready
    from .report_files import previous_month
    due = [x for x in lists(conn) if x["auto"] and split_addresses(x["recipients"])]
    if not due:
        return []
    now = now or now_utc()
    local = to_local(now)
    if local.day > CATCH_UP_DAYS or (local.day == 1 and local.hour < SEND_HOUR) or not mail_ready(conn):
        return []
    period = previous_month(local.date())
    out = []
    for lst in due:
        log = conn.execute("SELECT * FROM summary_mail_log WHERE period = ? AND list_id = ?",
                           (period, lst["id"])).fetchone()
        if log and (log["status"] == "sent" or log["attempts"] >= MAX_ATTEMPTS):
            continue
        if log and log["last_attempt"] and parse_utc(log["last_attempt"]) > now - RETRY_AFTER:
            continue
        try:
            to = send_list(conn, period, lst)
            _log(conn, period, lst["id"], True, ", ".join(to), when=now)
            out.append({"period": period, "list": lst["name"], "ok": True})
        except Exception as e:  # noqa: BLE001
            _log(conn, period, lst["id"], False, lst["recipients"], str(e), when=now)
            out.append({"period": period, "list": lst["name"], "ok": False, "error": str(e)})
    return out
