"use strict";
const $ = (id) => document.getElementById(id);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
const state = { me: null, users: [], settings: null, uid: null, month: null, year: null, openDay: null, timer: null };

// ---------- Hilfen ----------
// Session-Token zusätzlich zum Cookie: im DSM-Fenster (iframe) blocken Browser Cookies oft
const session = {
  get() { try { return sessionStorage.getItem("ze_session") || ""; } catch (e) { return session.mem || ""; } },
  set(v) { session.mem = v; try { v ? sessionStorage.setItem("ze_session", v) : sessionStorage.removeItem("ze_session"); } catch (e) { /* egal */ } },
};
function authHeaders(extra = {}) {
  const t = session.get();
  const h = t ? { ...extra, "X-Session-Token": t } : { ...extra };
  if (SYNO_TOKEN) h["X-SYNO-TOKEN"] = SYNO_TOKEN;
  return h;
}
// Im DSM-Fenster läuft alles über app.cgi?p=… (gleicher Ursprung wie DSM, Single Sign-on)
const PROXY = window.ZE_PROXY || null;
const SYNO_TOKEN = (() => { try { return new URLSearchParams(location.search).get("SynoToken") || ""; } catch (e) { return ""; } })();
function url(path) {
  if (!PROXY) return path;
  return `${PROXY}?${SYNO_TOKEN ? "SynoToken=" + encodeURIComponent(SYNO_TOKEN) + "&" : ""}p=${encodeURIComponent("/" + path)}`;
}
async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const extra = opts.body ? { "Content-Type": "application/json" } : {};
  let sendMethod = method;
  if (PROXY && ["PATCH", "PUT", "DELETE"].includes(method)) {
    // DSM reicht diese Methoden nicht an app.cgi durch → als POST mit Override schicken
    sendMethod = "POST";
    extra["X-HTTP-Method-Override"] = method;
  }
  let full = "api/v1/" + path;
  if (method === "GET") full += (full.includes("?") ? "&" : "?") + "_=" + Date.now();  // nie aus dem Cache
  const res = await fetch(url(full), {
    credentials: "same-origin",
    cache: "no-store",
    method: sendMethod,
    headers: authHeaders(extra),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401 && path !== "auth/login") { session.set(""); showLogin(); throw new Error("Nicht angemeldet"); }
  const data = (res.headers.get("content-type") || "").includes("json") ? await res.json() : null;
  if (!res.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map(x => x.msg).join(", ") : "Fehler " + res.status);
  }
  return data;
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const p2 = (n) => String(n).padStart(2, "0");
const isoDay = (d) => `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
const todayIso = () => isoDay(new Date());
const deDate = (iso) => iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}` : "";
const deShort = (iso) => iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.` : "";
const fmtDays = (n) => (Math.round(n * 2) / 2).toLocaleString("de-DE");
const signed = (s, txt) => `<span class="${s < 0 ? "neg" : s > 0 ? "pos" : ""}">${s > 0 ? "+" : ""}${txt}</span>`;
function minToHm(m) { const s = m < 0 ? "-" : m > 0 ? "+" : ""; m = Math.abs(m || 0); return `${s}${Math.floor(m / 60)}:${p2(m % 60)}`; }
function hmToMin(t) {
  t = (t || "").trim();
  if (!t) return 0;
  const m = t.match(/^([+-]?)(\d+)(?::(\d{1,2}))?$/);
  if (!m) throw new Error("Übertrag als Stunden:Minuten angeben, z. B. +3:30 oder -1:15");
  const v = Number(m[2]) * 60 + Number(m[3] || 0);
  return m[1] === "-" ? -v : v;
}
const okMsg = (el, text) => { el.innerHTML = `<span class="pos">${esc(text)}</span>`; setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000); };
const isAdmin = () => state.me && state.me.is_admin;
const store = { get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
                set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* egal */ } } };

// ---------- Start, Login, Reiter ----------
function showLogin() {
  $("appView").classList.add("hidden"); $("loginView").classList.remove("hidden");
  clearInterval(state.timer);
}

async function login() {
  $("loginErr").textContent = "";
  try {
    const body = { username: $("lu").value, password: $("lp").value };
    if (!$("otpWrap").classList.contains("hidden")) body.otp_code = $("lo").value.trim();
    let r;
    try { r = await api("auth/login", { method: "POST", body }); }
    catch (e) {
      if (e.message === "otp_required") {
        $("otpWrap").classList.remove("hidden"); $("lo").focus();
        throw new Error("Bitte den Zwei-Faktor-Code deines DSM-Kontos eingeben.");
      }
      throw e;
    }
    session.set(r.session_token || "");
    $("lp").value = ""; $("lo").value = ""; $("otpWrap").classList.add("hidden");
    await start();
  } catch (e) { $("loginErr").textContent = e.message; }
}

async function start() {
  state.me = await api("me");
  state.settings = await api("settings");
  $("loginView").classList.add("hidden"); $("appView").classList.remove("hidden");
  $("who").textContent = state.me.display_name + (isAdmin() ? " · Admin" : "") + (state.me.via_dsm ? " · über DSM" : "");
  $("logoutBtn").classList.toggle("hidden", !!state.me.via_dsm);
  $("pwCard").classList.toggle("hidden", !state.me.has_password);
  $("docsHint").classList.toggle("hidden", !!PROXY);
  document.body.classList.toggle("is-admin", isAdmin());
  $$(".admin").forEach(el => el.classList.toggle("hidden", !isAdmin()));
  state.uid = state.uid || state.me.id;
  const now = new Date();
  state.month = state.month || `${now.getFullYear()}-${p2(now.getMonth() + 1)}`;
  state.year = state.year || now.getFullYear();
  if (isAdmin()) await loadUsers();
  $("ySelKind").innerHTML = Object.entries(state.settings.absence_kinds).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  const wanted = (location.hash || "").slice(1) || store.get("tab") || "home";
  await showTab(["home", "month", "year", "admin", "me"].includes(wanted) && (wanted !== "admin" || isAdmin()) ? wanted : "home");
  clearInterval(state.timer);
  state.timer = setInterval(() => { if (state.tab === "home") refreshHome().catch(() => {}); }, 60000);
}

async function showTab(name) {
  state.tab = name;
  $$("#tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".page").forEach(p => p.classList.toggle("hidden", p.dataset.page !== name));
  store.set("tab", name);
  history.replaceState(null, "", "#" + name);
  try {
    if (name === "home") await refreshHome();
    if (name === "month") await loadMonth();
    if (name === "year") await loadYear();
    if (name === "admin") await loadAdmin();
    if (name === "me") { loadMeMail(); await loadTokens(); }
  } catch (e) { if (e.message !== "Nicht angemeldet") console.error(e); }
}

async function loadUsers() {
  state.users = await api("users");
  const opts = state.users.map(u => `<option value="${u.id}">${esc(u.display_name)}</option>`).join("");
  $$(".userSel, .userSel2").forEach(sel => { sel.innerHTML = opts; sel.value = String(state.uid); sel.classList.remove("hidden"); });
}

// ---------- Übersicht ----------
async function refreshHome() {
  const st = await api("clock/status");
  $("state").textContent = st.status === "in" ? "Eingestempelt" : "Ausgestempelt";
  $("state").className = "state " + st.status;
  $("since").textContent = st.since ? "seit " + new Date(st.since).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) + " Uhr" : "";
  $("clockBtn").textContent = st.status === "in" ? "Gehen" : "Kommen";
  $("clockBtn").dataset.action = st.status === "in" ? "out" : "in";
  $("sToday").textContent = st.today_total + " h";

  const t = todayIso();
  const mine = await api(`reports/user?from=${t}&to=${t}`);
  const a = mine.account;
  $("sGleit").innerHTML = signed(a.gleitzeit_s, a.gleitzeit + " h");
  $("sUe").innerHTML = signed(a.ueberstunden_s, a.ueberstunden + " h");
  const v = mine.vacation;
  $("sVac").textContent = v ? `${fmtDays(v.remaining)} Tage` : "–";
  $("sVacLabel").textContent = v ? `Resturlaub ${v.year}` : "Resturlaub";
  $("sSince").textContent = `Konten seit ${deDate(a.since)}` + (a.carryover_s || a.carryover_overtime_s ? " inkl. Übertrag" : "") +
    (v && v.planned ? ` · ${fmtDays(v.planned)} Urlaubstage geplant` : "");

  if (isAdmin()) {
    const ov = await api(`reports/overview?from=${t}&to=${t}`);
    $("teamTable").innerHTML = `<thead><tr><th></th><th>Name</th><th class="num">Heute</th><th class="num">Gleitzeit</th><th class="num">Überstunden</th><th class="num">Resturlaub</th></tr></thead><tbody>` +
      ov.users.map(u => `<tr><td><span class="dot ${u.present ? "in" : ""}" title="${u.present ? "anwesend" : "nicht da"}"></span></td>
        <td>${esc(u.display_name)}</td><td class="num">${u.worked}</td>
        <td class="num">${signed(u.account_gleitzeit_s, u.account_gleitzeit)}</td>
        <td class="num">${signed(u.account_ueberstunden_s, u.account_ueberstunden)}</td>
        <td class="num">${u.vacation_remaining == null ? "" : fmtDays(u.vacation_remaining)}</td></tr>`).join("") + "</tbody>";
  }
}

async function clockAction() {
  const btn = $("clockBtn"); btn.disabled = true;
  try { await api("clock/" + btn.dataset.action, { method: "POST", body: { source: "web" } }); await refreshHome(); }
  catch (e) { alert(e.message); } finally { btn.disabled = false; }
}

// ---------- Monat ----------
function monthRange() {
  const [y, m] = state.month.split("-").map(Number);
  return { from: `${y}-${p2(m)}-01`, to: `${y}-${p2(m)}-${p2(new Date(y, m, 0).getDate())}` };
}

function shiftMonth(delta) {
  const [y, m] = state.month.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  state.month = `${d.getFullYear()}-${p2(d.getMonth() + 1)}`;
  state.openDay = null;
  loadMonth();
}

async function loadMonth() {
  $("mMonth").value = state.month;
  const { from, to } = monthRange();
  const [rep, entries] = await Promise.all([
    api(`reports/user?user_id=${state.uid}&from=${from}&to=${to}`),
    api(`entries?user_id=${state.uid}&from=${from}&to=${to}`),
  ]);
  state.monthEntries = Object.fromEntries(entries.map(e => [e.id, e]));
  state.monthDays = Object.fromEntries(rep.days.map(d => [d.date, d]));
  const t = rep.totals;
  $("mStats").innerHTML = `
    <div class="stat"><span class="muted">Ist / Soll</span><b>${t.worked} / ${t.target}</b></div>
    <div class="stat"><span class="muted">Saldo Monat</span><b>${signed(t.diff_s, t.diff)}</b></div>
    <div class="stat"><span class="muted">davon Gleitzeit / Überstunden</span><b>${signed(t.gleitzeit_s, t.gleitzeit)} / ${signed(t.ueberstunden_s, t.ueberstunden)}</b></div>
    <div class="stat"><span class="muted">Urlaub im Monat</span><b>${fmtDays(rep.days.reduce((s, d) => s + d.vacation_days, 0))} Tage</b></div>`;
  const today = todayIso();
  const rows = rep.days.map(d => {
    const off = (!d.workday || d.before_start) && !d.worked_s;
    const diff = d.diff_s ? signed(d.diff_s, d.diff) : "";
    const showUe = d.worked_s || d.target_s || d.overtime_day;
    const ue = isAdmin()
      ? (showUe ? `<button class="ue ${d.overtime_day ? "on" : ""}" data-ue="${d.date}" title="Zeit über dem Tagessoll ins Überstunden-Konto">Ü</button>` : "")
      : (d.overtime_day || d.absence === "mehrarbeit" ? `<span class="ue ${d.diff_ueberstunden_s ? "on" : ""}">Ü</span>` : "");
    const info = d.absence_label ? `<span class="tag k k-${esc(d.absence)}">${esc(d.absence_label)}</span>` :
      (d.before_start && d.workday ? '<span class="muted small">vor Erfassungsbeginn</span>' : "");
    const brk = (d.break_deducted_s ? ` <span class="muted small">−${Math.round(d.break_deducted_s / 60)} min Pause</span>` : "") +
      (d.travel_min ? ` <span class="tag" title="beim Stempeln dazugerechnete Wegezeit">+${d.travel_min} min Weg</span>` : "");
    const multi = d.segments > 1 ? ` <span class="muted small">(${d.segments}×)</span>` : "";
    const row = `<tr class="day ${off ? "weekend" : ""} ${d.date === today ? "today" : ""}" data-day="${d.date}">
      <td>${d.weekday} ${deShort(d.date)}</td><td>${d.first_in || ""}</td><td>${d.running ? "läuft" : d.last_out || ""}${multi}</td>
      <td class="num">${d.worked_s ? d.worked : ""}</td><td class="num">${d.target_s ? d.target : ""}</td>
      <td class="num">${diff}</td><td>${ue}</td><td>${info}${brk}</td></tr>`;
    return row + (state.openDay === d.date ? detailRow(d) : "");
  }).join("");
  $("mTable").innerHTML = `<thead><tr><th>Tag</th><th>Kommen</th><th>Gehen</th><th class="num">Ist</th><th class="num">Soll</th><th class="num">Saldo</th><th></th><th></th></tr></thead>
    <tbody>${rows}</tbody><tfoot><tr><th>Summe</th><th></th><th></th><th class="num">${t.worked}</th><th class="num">${t.target}</th><th class="num">${signed(t.diff_s, t.diff)}</th><th></th><th></th></tr></tfoot>`;
}

function detailRow(d) {
  const kinds = state.settings.absence_kinds;
  const entries = d.entry_ids.map(id => state.monthEntries[id]).filter(Boolean);
  const ent = entries.map(e => `
    <div class="row" data-entry="${e.id}">
      <input type="datetime-local" class="es" value="${e.start.slice(0, 16)}">
      <span>–</span>
      <input type="datetime-local" class="ee" value="${e.end ? e.end.slice(0, 16) : ""}" ${e.end ? "" : 'placeholder="läuft"'}>
      <input class="en" placeholder="Notiz" value="${esc(e.note || "")}" style="width:180px">
      <button class="mini" data-act="save-entry">Speichern</button>
      <button class="mini danger" data-act="del-entry">Löschen</button>
      <span class="muted small">${esc(e.source)}${e.travel_in_min || e.travel_out_min ? ` · Weg +${e.travel_in_min}/+${e.travel_out_min} min` : ""}${e.edited ? " · korrigiert" : ""}</span>
    </div>`).join("") || '<span class="muted small">Keine Buchungen an diesem Tag.</span>';
  const kindOpts = Object.entries(kinds).map(([k, v]) => `<option value="${k}" ${d.absence_id && d.absence === k ? "selected" : ""}>${esc(v)}</option>`).join("");
  return `<tr class="detail"><td colspan="8"><div class="detailbox">
    <b>${d.weekday} ${deDate(d.date)}${d.holiday ? " · " + esc(d.holiday) : ""}</b>
    ${ent}
    <div class="row">
      <span class="muted small">Nachtragen:</span>
      <input type="time" class="ns" value="08:00"> <span>–</span> <input type="time" class="ne" value="16:00">
      <input class="nn" placeholder="Notiz" style="width:180px">
      <button class="mini primary" data-act="add-entry">Hinzufügen</button>
      <span class="muted small">Ende vor Beginn = über Mitternacht</span>
    </div>
    <div class="row">
      <span class="muted small">Abwesenheit:</span>
      <select class="ak">${kindOpts}</select>
      <label class="inline"><input type="checkbox" class="ah" ${d.half_day ? "checked" : ""}> halber Tag</label>
      <button class="mini" data-act="set-abs">${d.absence_id ? "Ändern" : "Eintragen"}</button>
      ${d.absence_id ? '<button class="mini danger" data-act="del-abs">Entfernen</button>' : ""}
    </div>
    <div class="err" data-err></div>
  </div></td></tr>`;
}

async function onMonthClick(e) {
  const ueBtn = e.target.closest("[data-ue]");
  if (ueBtn) {
    e.stopPropagation();
    await api("dayflags", { method: "PUT", body: { user_id: state.uid, day: ueBtn.dataset.ue, overtime: !ueBtn.classList.contains("on") } });
    return loadMonth();
  }
  const actBtn = e.target.closest("[data-act]");
  if (actBtn) return detailAction(actBtn);
  if (e.target.closest("tr.detail")) return;
  const tr = e.target.closest("tr.day");
  if (!tr || !isAdmin()) return;
  state.openDay = state.openDay === tr.dataset.day ? null : tr.dataset.day;
  loadMonth();
}

async function detailAction(btn) {
  const box = btn.closest(".detailbox");
  const errEl = box.querySelector("[data-err]");
  const day = state.openDay;
  errEl.textContent = "";
  try {
    const act = btn.dataset.act;
    if (act === "save-entry" || act === "del-entry") {
      const row = btn.closest("[data-entry]"); const id = row.dataset.entry;
      if (act === "del-entry") {
        if (!confirm("Buchung wirklich löschen?")) return;
        await api("entries/" + id, { method: "DELETE" });
      } else {
        const body = { start: row.querySelector(".es").value, note: row.querySelector(".en").value };
        const end = row.querySelector(".ee").value;
        if (end) body.end = end;
        await api("entries/" + id, { method: "PATCH", body });
      }
    } else if (act === "add-entry") {
      const s = box.querySelector(".ns").value, en = box.querySelector(".ne").value;
      if (!s || !en) throw new Error("Bitte Kommen und Gehen angeben");
      let endDay = day;
      if (en <= s) { const d = new Date(day + "T12:00"); d.setDate(d.getDate() + 1); endDay = isoDay(d); }
      await api("entries", { method: "POST", body: { user_id: state.uid, start: `${day}T${s}`, end: `${endDay}T${en}`, note: box.querySelector(".nn").value || null } });
    } else if (act === "set-abs") {
      await api("absences", { method: "POST", body: { user_id: state.uid, from_day: day, kind: box.querySelector(".ak").value, half_day: box.querySelector(".ah").checked } });
    } else if (act === "del-abs") {
      await api("absences/" + state.monthDays[day].absence_id, { method: "DELETE" });
    }
    await loadMonth();
  } catch (e) { errEl.textContent = e.message; }
}

// ---------- Jahr ----------
const sel = { a: null, b: null, dragging: false, pending: false, ptype: "mouse" };
const MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"];

async function loadYear() {
  $("yLabel").textContent = state.year;
  const rep = await api(`reports/year?user_id=${state.uid}&year=${state.year}`);
  state.yearDays = Object.fromEntries(rep.days.map(d => [d.date, d]));
  const kinds = state.settings.absence_kinds;
  $("yLegend").innerHTML = Object.entries(kinds).map(([k, v]) => `<span><i class="k-${k}"></i>${esc(v)}</span>`).join("") +
    '<span><i style="background:var(--weekend)"></i>frei / Wochenende</span>' +
    '<span><i style="background:radial-gradient(circle at 50% 75%, var(--accent) 2px, var(--card) 2.5px)"></i>gearbeitet</span>';
  const today = todayIso();
  let html = "";
  for (let m = 0; m < 12; m++) {
    const first = new Date(state.year, m, 1);
    const lead = (first.getDay() + 6) % 7;
    const days = new Date(state.year, m + 1, 0).getDate();
    let cells = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"].map(w => `<div class="wd">${w}</div>`).join("");
    cells += "<div></div>".repeat(lead);
    for (let d = 1; d <= days; d++) {
      const iso = `${state.year}-${p2(m + 1)}-${p2(d)}`;
      const info = state.yearDays[iso] || {};
      const cls = ["cell", "d"];
      if (!info.workday) cls.push("we");
      if (info.absence) cls.push("k-" + info.absence);
      if (info.half_day) cls.push("half");
      if (info.worked_s) cls.push("worked");
      if (info.before_start) cls.push("pre");
      if (iso === today) cls.push("today");
      const title = [deDate(iso), info.absence_label || info.holiday, info.worked_s ? info.worked + " h gearbeitet" : ""].filter(Boolean).join(" · ");
      cells += `<div class="${cls.join(" ")}" data-date="${iso}" title="${esc(title)}">${d}</div>`;
    }
    html += `<div class="mon"><h4>${MONTHS[m]}</h4><div class="g">${cells}</div></div>`;
  }
  $("yGrid").innerHTML = html;
  paintSel();
  // Auf dem Handy direkt zum aktuellen Monat springen
  if (window.innerWidth < 720 && state.year === new Date().getFullYear() && !state.yearScrolled) {
    state.yearScrolled = true;
    const mon = $$("#yGrid .mon")[new Date().getMonth()];
    if (mon) mon.scrollIntoView({ block: "start" });
  }
  const v = rep.vacation, ad = rep.absence_days;
  $("yStats").innerHTML = (v ? `
    <div class="stat"><span class="muted">Urlaub ${v.year}: Anspruch + Übertrag</span><b>${fmtDays(v.entitlement)} + ${fmtDays(v.carryover)}</b></div>
    <div class="stat"><span class="muted">genommen / geplant</span><b>${fmtDays(v.taken)} / ${fmtDays(v.planned)}</b></div>
    <div class="stat"><span class="muted">Resturlaub</span><b>${signed(v.remaining, fmtDays(v.remaining) + " Tage")}</b></div>` : "") +
    Object.entries(kinds).filter(([k]) => k !== "urlaub" && ad[k]).map(([k, lab]) =>
      `<div class="stat"><span class="muted">${esc(lab)}</span><b>${fmtDays(ad[k])} Tage</b></div>`).join("");
}

function selRange() {
  if (!sel.a) return null;
  const b = sel.b || sel.a;
  return sel.a <= b ? [sel.a, b] : [b, sel.a];
}

function paintSel() {
  const r = selRange();
  $$("#yGrid .cell.d").forEach(c => c.classList.toggle("sel", !!r && c.dataset.date >= r[0] && c.dataset.date <= r[1]));
}

function showSelBar() {
  const r = selRange();
  if (!r) { $("ySel").classList.add("hidden"); return; }
  const dates = Object.keys(state.yearDays).filter(d => d >= r[0] && d <= r[1]);
  const work = dates.filter(d => state.yearDays[d].workday && state.yearDays[d].absence !== "feiertag").length;
  $("ySelText").innerHTML = (r[0] === r[1] ? `<b>${deDate(r[0])}</b>` : `<b>${deShort(r[0])} – ${deDate(r[1])}</b>`) +
    ` · ${dates.length} Tag${dates.length === 1 ? "" : "e"}, davon ${work} Arbeitstag${work === 1 ? "" : "e"}` +
    (sel.pending ? ' <span class="muted small">– letzten Tag antippen oder direkt eintragen</span>' : "");
  $("ySelHalf").checked = false;
  $("ySel").classList.remove("hidden");
}

function clearSel() { sel.a = sel.b = null; sel.pending = false; paintSel(); $("ySel").classList.add("hidden"); }

function setupYearSelection() {
  const grid = $("yGrid");
  grid.addEventListener("pointerdown", e => {
    sel.ptype = e.pointerType;
    const c = e.target.closest(".cell.d");
    if (!c || !isAdmin() || e.pointerType !== "mouse") return;
    e.preventDefault();
    sel.dragging = true; sel.pending = false; sel.a = sel.b = c.dataset.date; paintSel();
  });
  grid.addEventListener("pointerover", e => {
    if (!sel.dragging) return;
    const c = e.target.closest(".cell.d");
    if (c && c.dataset.date !== sel.b) { sel.b = c.dataset.date; paintSel(); }
  });
  document.addEventListener("pointerup", () => { if (sel.dragging) { sel.dragging = false; showSelBar(); } });
  grid.addEventListener("click", e => {
    if (!isAdmin() || sel.ptype === "mouse") return;
    const c = e.target.closest(".cell.d");
    if (!c) return;
    if (!sel.pending) { sel.a = sel.b = c.dataset.date; sel.pending = true; }
    else { sel.b = c.dataset.date; sel.pending = false; }
    paintSel(); showSelBar();
  });
  $("ySelCancel").onclick = clearSel;
  $("ySelSet").onclick = async () => {
    const r = selRange(); $("yErr").textContent = "";
    try {
      const res = await api("absences", { method: "POST", body: { user_id: state.uid, from_day: r[0], to_day: r[1], kind: $("ySelKind").value, half_day: $("ySelHalf").checked } });
      clearSel(); await loadYear();
      okMsg($("yErr"), `${res.days} Tag${res.days === 1 ? "" : "e"} eingetragen` + (res.skipped ? `, ${res.skipped} freie Tage übersprungen` : ""));
    } catch (e) { $("yErr").textContent = e.message; }
  };
  $("ySelDel").onclick = async () => {
    const r = selRange(); $("yErr").textContent = "";
    try {
      const res = await api(`absences?user_id=${state.uid}&from=${r[0]}&to=${r[1]}`, { method: "DELETE" });
      clearSel(); await loadYear();
      okMsg($("yErr"), `${res.days} Eintrag/Einträge entfernt`);
    } catch (e) { $("yErr").textContent = e.message; }
  };
  document.addEventListener("keydown", e => { if (e.key === "Escape") clearSel(); });
}

// ---------- Verwaltung ----------
async function loadAdmin() {
  await loadUsers();
  const ov = await api("reports/overview?from=" + todayIso() + "&to=" + todayIso());
  const bal = Object.fromEntries(ov.users.map(u => [u.id, u]));
  $("usersTable").innerHTML = `<thead><tr><th>Name</th><th>Wochenarbeitszeit</th><th>Erfassungsbeginn</th>
      <th>Übertrag GZ</th><th>Übertrag Ü</th><th class="num">Urlaub/Jahr</th><th class="num">Resturlaub-Übertrag</th>
      <th>E-Mail (Berichte)</th><th>Neues Passwort</th><th class="num">Gleitzeit</th><th class="num">Überst.</th><th class="num">Urlaub Rest</th><th></th></tr></thead><tbody>` +
    state.users.map(u => {
      const b = bal[u.id] || {};
      return `<tr data-id="${u.id}">
        <td>${esc(u.display_name)} <span class="muted small">${esc(u.username)}</span>${u.is_admin ? ' <span class="tag">Admin</span>' : ""}${u.has_password ? "" : ' <span class="tag" title="meldet sich mit dem DSM-Konto an">DSM</span>'}</td>
        <td>${fmtNum(u.weekly_hours)} h · ${daysLabel(u.workdays)} <button class="mini usched" title="Wochenarbeitszeit mit gilt-ab-Datum ändern">Verlauf</button></td>
        <td><input class="us" type="date" value="${u.tracking_start || ""}"></td>
        <td><input class="uc w80" value="${minToHm(u.carryover_minutes)}"></td>
        <td><input class="uo w80" value="${minToHm(u.carryover_overtime_minutes)}"></td>
        <td class="num"><input class="uv w64" type="number" step="0.5" min="0" max="60" value="${u.vacation_days_per_year}"></td>
        <td class="num"><input class="ur w64" type="number" step="0.5" value="${u.carryover_vacation}"></td>
        <td><input class="umail" type="email" value="${esc(u.email || "")}" placeholder="keine" style="width:170px">
          <label class="inline small" style="margin-top:3px" title="Monats-/Jahresberichte automatisch per Mail"><input type="checkbox" class="urm"${u.report_mail ? " checked" : ""}> auto</label></td>
        <td><input class="up" type="password" placeholder="unverändert" style="width:120px" autocomplete="new-password"></td>
        <td class="num">${b.account_gleitzeit != null ? signed(b.account_gleitzeit_s, b.account_gleitzeit) : ""}</td>
        <td class="num">${b.account_ueberstunden != null ? signed(b.account_ueberstunden_s, b.account_ueberstunden) : ""}</td>
        <td class="num">${b.vacation_remaining != null ? fmtDays(b.vacation_remaining) : ""}</td>
        <td><button class="mini usave">Speichern</button></td></tr>`;
    }).join("") + "</tbody>";
  await loadSettingsForm();
}

const WD = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
const fmtNum = (n) => String(n).replace(".", ",");
function daysLabel(days) {
  days = (days || []).slice().sort();
  if (days.join() === "1,2,3,4,5") return "Mo–Fr";
  const consecutive = days.every((d, i) => i === 0 || d === days[i - 1] + 1);
  return consecutive && days.length > 2 ? `${WD[days[0] - 1]}–${WD[days[days.length - 1] - 1]}` : days.map(d => WD[d - 1]).join(", ");
}

async function toggleSchedules(tr) {
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("sched")) { next.remove(); return; }
  const uid = tr.dataset.id;
  const list = await api(`users/${uid}/schedules`);
  const row = document.createElement("tr");
  row.className = "detail sched"; row.dataset.uid = uid;
  const today = isoDay(new Date());
  row.innerHTML = `<td colspan="12"><div class="detailbox">
    <b>Wochenarbeitszeit – Verlauf</b>
    <table style="width:auto"><tbody>${list.map(x => `<tr><td>${x.valid_from ? "ab " + deDate(x.valid_from) : "von Anfang an"}</td>
      <td class="num">${fmtNum(x.weekly_hours)} h</td><td>${daysLabel(x.workdays)}</td>
      <td>${x.valid_from ? `<button class="mini danger" data-sact="del" data-sid="${x.id}">Löschen</button>` : ""}</td>
      <td>${x.current ? '<span class="tag">gilt heute</span>' : ""}</td></tr>`).join("")}</tbody></table>
    <div class="row">
      <label>gilt ab <input type="date" class="sf" value="${today}"></label>
      <label>Std./Woche <input type="number" class="sh w80" step="0.5" min="0" max="80" value="${list[list.length - 1].weekly_hours}"></label>
      <span class="muted small">Arbeitstage:</span>
      ${WD.map((w, i) => `<label class="inline"><input type="checkbox" class="sd" value="${i + 1}" ${list[list.length - 1].workdays.includes(i + 1) ? "checked" : ""}> ${w}</label>`).join("")}
      <button class="mini primary" data-sact="add">Hinzufügen</button>
    </div>
    <span class="muted small">Ändert die Arbeitszeit ab dem gewählten Tag. Tage davor werden weiter mit der alten Arbeitszeit gerechnet. Gleiches Datum ersetzt einen Eintrag.</span>
    <div class="err" data-serr></div>
  </div></td>`;
  tr.after(row);
}

async function scheduleAction(btn) {
  const row = btn.closest("tr.sched"); const uid = row.dataset.uid;
  const err = row.querySelector("[data-serr]"); err.textContent = "";
  try {
    if (btn.dataset.sact === "del") {
      if (!confirm("Diesen Eintrag löschen? Ab dem Datum gilt dann wieder der vorherige.")) return;
      await api(`users/${uid}/schedules/${btn.dataset.sid}`, { method: "DELETE" });
    } else {
      const days = $$(".sd", row).filter(c => c.checked).map(c => Number(c.value));
      if (!days.length) throw new Error("Bitte mindestens einen Arbeitstag wählen");
      await api(`users/${uid}/schedules`, { method: "POST", body: {
        valid_from: row.querySelector(".sf").value, weekly_hours: Number(row.querySelector(".sh").value), workdays: days } });
    }
    await loadAdmin();
    const tr = $("usersTable").querySelector(`tr[data-id="${uid}"]`);
    if (tr) await toggleSchedules(tr);
  } catch (e) { err.textContent = e.message; }
}

async function saveUserRow(btn) {
  const tr = btn.closest("tr"); $("usersErr").textContent = "";
  try {
    const body = {
      tracking_start: tr.querySelector(".us").value || null,
      carryover_minutes: hmToMin(tr.querySelector(".uc").value),
      carryover_overtime_minutes: hmToMin(tr.querySelector(".uo").value),
      vacation_days_per_year: Number(tr.querySelector(".uv").value),
      carryover_vacation: Number(tr.querySelector(".ur").value),
      email: tr.querySelector(".umail").value.trim() || null,
      report_mail: tr.querySelector(".urm").checked,
    };
    const pw = tr.querySelector(".up").value;
    if (pw) body.password = pw;
    await api("users/" + tr.dataset.id, { method: "PATCH", body });
    if (Number(tr.dataset.id) === state.me.id) state.me = await api("me");
    await loadAdmin();
    okMsg($("usersErr"), "Gespeichert.");
  } catch (e) { $("usersErr").textContent = e.message; }
}

async function newUser() {
  $("newUserErr").textContent = "";
  try {
    await api("users", { method: "POST", body: {
      username: $("nu").value.trim(), display_name: $("nd").value.trim(), password: $("np").value,
      weekly_hours: Number($("nh").value), vacation_days_per_year: Number($("nv").value),
      tracking_start: $("ns").value || null, email: $("ne").value.trim() || null } });
    ["nu", "nd", "np", "ns", "ne"].forEach(id => $(id).value = "");
    await loadAdmin();
    okMsg($("newUserErr"), "Angelegt.");
  } catch (e) { $("newUserErr").textContent = e.message; }
}

async function loadSettingsForm() {
  const s = state.settings = await api("settings");
  $("setBl").innerHTML = Object.entries(s.bundeslaender).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("setBl").value = s.bundesland; $("setFree").value = s.company_free_days;
  $("setUrl").value = s.public_url || "";
  $("setTravIn").value = s.travel_in_minutes; $("setTravOut").value = s.travel_out_minutes;
  $$(".travSrc").forEach(c => { c.checked = (s.travel_sources || []).includes(c.value); });
  $("setDsmLogin").checked = s.dsm_login; $("setDsmCreate").checked = s.dsm_auto_create;
  $("setDsmAdmins").checked = s.dsm_admins; $("setDsmUrl").value = s.dsm_url || "";
  // im DSM-Fenster zeigt location auf DSM (Port 5000/5001) – die App selbst läuft auf ihrem eigenen Port
  $("setUrl").placeholder = s.public_url_env || "leer = nur im WLAN: " +
    (PROXY ? `http://${location.hostname}:${s.app_port}` : location.origin);
  $("setBreak").innerHTML = Object.entries(s.break_modes).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("setBreak").value = s.break_mode; $("setMin").value = s.break_minutes;
  $("setMinWrap").classList.toggle("hidden", s.break_mode !== "fixed");
  // Mailversand
  $("mailMonthly").checked = s.mail_monthly; $("mailYearly").checked = s.mail_yearly;
  $$(".mailFmt").forEach(c => { c.checked = (s.mail_formats || []).includes(c.value); });
  $("smtpHost").value = s.smtp_host || ""; $("smtpPort").value = s.smtp_port || 587;
  $("smtpSec").value = s.smtp_security || "starttls"; $("smtpUser").value = s.smtp_user || "";
  $("smtpPw").value = ""; $("smtpPw").placeholder = s.smtp_password_set ? "gespeichert – leer = unverändert" : "";
  $("smtpFrom").value = s.smtp_from || ""; $("smtpFromName").value = s.smtp_from_name || "";
  if (!$("mailTestTo").value) $("mailTestTo").value = state.me.email || "";
  loadMailLog().catch(() => {});
  const h = await api("holidays");
  $("holYear").textContent = `${h.year} · ${esc(h.bundesland_name)}`;
  $("holTable").innerHTML = "<tbody>" + h.days.map(d => `<tr><td>${d.weekday} ${deDate(d.date)}</td><td>${esc(d.name)}</td></tr>`).join("") + "</tbody>";
}

async function saveSettings() {
  $("setErr").textContent = "";
  try {
    await api("settings", { method: "PATCH", body: { bundesland: $("setBl").value, company_free_days: $("setFree").value,
      break_mode: $("setBreak").value, break_minutes: Number($("setMin").value || 0), public_url: $("setUrl").value.trim(),
      dsm_login: $("setDsmLogin").checked, dsm_auto_create: $("setDsmCreate").checked, dsm_admins: $("setDsmAdmins").checked,
      dsm_url: $("setDsmUrl").value.trim() || null,
      travel_in_minutes: Number($("setTravIn").value || 0), travel_out_minutes: Number($("setTravOut").value || 0),
      travel_sources: $$(".travSrc").filter(c => c.checked).map(c => c.value) } });
    await loadSettingsForm();
    okMsg($("setErr"), "Gespeichert.");
  } catch (e) { $("setErr").textContent = e.message; }
}

// Mailversand
async function saveMail() {
  $("mailErr").textContent = "";
  try {
    const body = { mail_monthly: $("mailMonthly").checked, mail_yearly: $("mailYearly").checked,
      mail_formats: $$(".mailFmt").filter(c => c.checked).map(c => c.value),
      smtp_host: $("smtpHost").value.trim(), smtp_port: Number($("smtpPort").value || 587), smtp_security: $("smtpSec").value,
      smtp_user: $("smtpUser").value.trim(), smtp_from: $("smtpFrom").value.trim(), smtp_from_name: $("smtpFromName").value.trim() };
    if ($("smtpPw").value) body.smtp_password = $("smtpPw").value;
    await api("settings", { method: "PATCH", body });
    await loadSettingsForm();
    okMsg($("mailErr"), "Gespeichert.");
  } catch (e) { $("mailErr").textContent = e.message; }
}
async function testMail() {
  $("mailErr").textContent = "";
  const to = $("mailTestTo").value.trim();
  if (!to) { $("mailErr").textContent = "Bitte eine Adresse für die Testmail eintragen."; return; }
  $("mailTest").disabled = true; $("mailTest").textContent = "Sende …";
  try { const r = await api("settings/mail-test", { method: "POST", body: { to } }); okMsg($("mailErr"), r.message); }
  catch (e) { $("mailErr").textContent = e.message + " (Einstellungen vorher speichern?)"; }
  finally { $("mailTest").disabled = false; $("mailTest").textContent = "Testmail senden"; }
}
async function loadMailLog() {
  const rows = await api("reports/mail-log?limit=50");
  const fmt = (iso) => iso ? new Date(iso).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "";
  $("mailLog").innerHTML = rows.length ? `<thead><tr><th>Bericht</th><th>Mitarbeiter</th><th>an</th><th>Status</th><th>zuletzt</th></tr></thead><tbody>` +
    rows.map(r => `<tr><td>${esc(r.period)}</td><td>${esc(r.display_name)}</td><td>${esc(r.recipient || "")}</td>
      <td>${r.status === "sent" ? '<span class="pos">verschickt</span>' : `<span class="neg">Fehler (${r.attempts}×)</span> <span class="muted small">${esc(r.error || "")}</span>`}</td>
      <td>${fmt(r.sent_at || r.last_attempt)}</td></tr>`).join("") + "</tbody>"
    : '<tbody><tr><td class="muted">Noch keine automatischen Berichte verschickt.</td></tr></tbody>';
}

// Berichte: Download und Mail
async function downloadFile(path, fallbackName) {
  // per fetch statt Link, damit es auch im DSM-Fenster mit Header-Login klappt
  const res = await fetch(url(path), { credentials: "same-origin", cache: "no-store", headers: authHeaders() });
  if (!res.ok) {
    let msg = "Fehler " + res.status;
    try { const d = await res.json(); if (typeof d.detail === "string") msg = d.detail; } catch (e) { /* egal */ }
    throw new Error("Download fehlgeschlagen: " + msg);
  }
  const name = ((res.headers.get("content-disposition") || "").match(/filename="([^"]+)"/) || [])[1] || fallbackName;
  const blobUrl = URL.createObjectURL(await res.blob());
  const a = document.createElement("a"); a.href = blobUrl; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 4000);
}
const reportPeriod = (scope) => scope === "year" ? String(state.year) : state.month;
const reportMsgEl = (scope) => scope === "year" ? $("yErr") : $("mMsg");
async function downloadReport(btn) {
  const scope = btn.dataset.scope, fmt = btn.dataset.fmt, el = reportMsgEl(scope);
  el.textContent = ""; btn.disabled = true;
  try { await downloadFile(`api/v1/reports/download?user_id=${state.uid}&period=${reportPeriod(scope)}&format=${fmt}`, `bericht.${fmt}`); }
  catch (e) { el.textContent = e.message; }
  finally { btn.disabled = false; }
}
async function mailReport(btn) {
  const scope = btn.dataset.scope, el = reportMsgEl(scope), period = reportPeriod(scope);
  el.textContent = "";
  const person = (state.users || []).find(u => u.id === state.uid) || (state.uid === state.me.id ? state.me : {});
  if (!person.email) {
    el.textContent = state.uid === state.me.id
      ? "Für dich ist keine E-Mail-Adresse hinterlegt – bitte unter „Mein Zugang“ eintragen."
      : `Für ${person.display_name || "diese Person"} ist keine E-Mail-Adresse hinterlegt – bitte unter Verwaltung → Mitarbeiter eintragen.`;
    return;
  }
  const what = scope === "year" ? `Jahresübersicht ${period}`
    : `Arbeitszeitnachweis ${new Date(period + "-01T12:00").toLocaleDateString("de-DE", { month: "long", year: "numeric" })}`;
  if (!confirm(`${what} an ${person.email} schicken?`)) return;
  btn.disabled = true;
  try { const r = await api("reports/mail", { method: "POST", body: { period, user_id: state.uid } }); okMsg(el, r.message); }
  catch (e) { el.textContent = e.message; }
  finally { btn.disabled = false; }
}

// Mein Zugang: eigene Mailadresse
function loadMeMail() {
  const s = state.settings || {};
  $("meEmail").value = state.me.email || "";
  $("meReportMail").checked = state.me.report_mail !== false;
  const auto = [s.mail_monthly && "Monatsbericht am 1. des Monats", s.mail_yearly && "Jahresbericht im Januar"].filter(Boolean);
  $("meMailInfo").textContent = auto.length
    ? `Automatisch: ${auto.join(" und ")}. Einzelne Berichte kannst du jederzeit unter „Monat“ oder „Jahr“ mit ✉ Mail schicken.`
    : "Der automatische Versand ist noch nicht eingeschaltet (Admin: Verwaltung → Berichte per Mail). Mit ✉ Mail unter „Monat“ oder „Jahr“ geht es jederzeit von Hand.";
}
async function saveMeMail() {
  $("meMailErr").textContent = "";
  try {
    state.me = await api("me", { method: "PATCH", body: { email: $("meEmail").value.trim(), report_mail: $("meReportMail").checked } });
    loadMeMail();
    okMsg($("meMailErr"), "Gespeichert.");
  } catch (e) { $("meMailErr").textContent = e.message; }
}

// Import
const STATUS = { neu: "neu", doppelt: "schon vorhanden", konflikt: "Konflikt", fehler: "Fehler", "übersprungen": "übersprungen" };
async function runImport(commit) {
  $("impErr").textContent = "";
  const f = $("impFile").files[0];
  if (!f) { $("impErr").textContent = "Bitte eine CSV-Datei auswählen."; return; }
  try {
    const text = await f.text();
    const res = await api("import", { method: "POST", body: { user_id: Number($("impUser").value), kind: $("impKind").value, csv_text: text, commit } });
    const neu = res.summary.neu || 0;
    $("impSummary").innerHTML = Object.entries(res.summary).map(([k, v]) => `<span class="tag st-${esc(k)}">${v} ${esc(STATUS[k] || k)}</span>`).join(" ") +
      (commit ? ` <b class="pos">→ ${res.imported} übernommen</b>` : "");
    $("impCommit").textContent = `${neu} übernehmen`;
    $("impCommit").disabled = commit || neu === 0;
    const what = (r) => r.start ? `${esc(r.start)} – ${esc(r.end || "")}${r.hours ? ` (${String(r.hours).replace(".", ",")} h)` : ""}`
      : `${deDate(r.day)} ${r.label ? esc(r.label) : ""}${r.half_day ? " ½" : ""}`;
    $("impTable").innerHTML = `<thead><tr><th>Zeile</th><th>Status</th><th>Inhalt</th><th>Notiz / Hinweis</th></tr></thead><tbody>` +
      res.rows.map(r => `<tr><td class="muted">${r.line}</td><td class="st-${esc(r.status)}">${esc(STATUS[r.status] || r.status)}</td>
        <td>${r.start || r.day ? what(r) : `<code>${esc(r.raw)}</code>`}</td><td>${esc(r.message || r.note || "")}</td></tr>`).join("") + "</tbody>";
    $("impResult").classList.remove("hidden");
  } catch (e) { $("impErr").textContent = e.message; }
}

// ---------- Mein Zugang ----------
async function makeQr() {
  const uid = isAdmin() ? Number($("qrUser").value) : state.me.id;
  const path = uid === state.me.id ? "me/tokens" : `users/${uid}/tokens`;
  const r = await api(path, { method: "POST", body: { label: "iPhone Kurzbefehl", host_hint: location.hostname } });
  const lanOnly = !r.server_is_public;
  $("qrBox").innerHTML = `<div>${r.qr_svg.replace(/<\?xml[^>]*>/, "")}</div>
    <div><b>Mit der iPhone-Kamera scannen.</b> Es öffnet sich eine persönliche Einrichtungsseite mit
      Test-Knopf, Kopier-Knöpfen und Anleitung für die Kurzbefehle „Kommen“ und „Gehen“.<br><br>
      <a class="btn" href="${esc(r.setup_url)}" target="_blank" rel="noopener noreferrer">Einrichtungsseite öffnen</a>
      <button class="mini" id="qrCopy" style="margin-left:6px">Link kopieren</button><br><br>
      <span class="muted small">Adresse im Link: <b>${esc(r.setup.server)}</b>
      ${lanOnly ? `<br><span class="neg">Funktioniert nur im WLAN.</span> Für unterwegs eine https-Adresse (Reverse Proxy) unter
        ${isAdmin() ? "Verwaltung → Einstellungen → Öffentliche Adresse" : "den Einstellungen (Admin)"} eintragen und neuen QR-Code erzeugen.` :
        (isAdmin() ? " – ändern unter Verwaltung → Einstellungen → Öffentliche Adresse" : "")}</span><br>
      <span class="muted small">Der Link ist ein persönlicher Schlüssel – nicht weitergeben. Jeder neue QR-Code erzeugt einen
        weiteren Schlüssel; alte bleiben gültig, bis der Admin sie sperrt.</span></div>`;
  $("qrBox").classList.remove("hidden");
  loadTokens().catch(() => {});
  $("qrCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(r.setup_url); $("qrCopy").textContent = "Kopiert ✓"; }
    catch (e) { prompt("Link kopieren:", r.setup_url); }
  };
}

function tokenOwner() { return isAdmin() ? Number($("qrUser").value) : state.me.id; }
async function loadTokens() {
  const uid = tokenOwner();
  const base = uid === state.me.id ? "me/tokens" : `users/${uid}/tokens`;
  const list = await api(base);
  const fmt = (iso) => iso ? new Date(iso).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" }) : "noch nie";
  $("tokTable").innerHTML = list.length ? `<thead><tr><th>Bezeichnung</th><th>erstellt</th><th>zuletzt benutzt</th><th></th></tr></thead><tbody>` +
    list.map(t => `<tr><td>${esc(t.label)}</td><td>${fmt(t.created_at)}</td><td>${fmt(t.last_used_at)}</td>
      <td><button class="mini danger" data-tok="${t.id}">Sperren</button></td></tr>`).join("") + "</tbody>"
    : '<tbody><tr><td class="muted">Noch keine Zugänge – oben einen QR-Code erzeugen.</td></tr></tbody>';
  $$("#tokTable [data-tok]").forEach(b => b.onclick = async () => {
    if (!confirm("Diesen Zugang sperren? Kurzbefehle mit diesem Schlüssel funktionieren dann nicht mehr.")) return;
    try { await api(`${base}/${b.dataset.tok}`, { method: "DELETE" }); await loadTokens(); okMsg($("tokErr"), "Gesperrt."); }
    catch (e) { $("tokErr").textContent = e.message; }
  });
}

async function changePw() {
  $("pwErr").textContent = "";
  try {
    await api("me/password", { method: "POST", body: { old_password: $("pwOld").value, new_password: $("pwNew").value } });
    okMsg($("pwErr"), "Geändert. Bitte neu anmelden.");
    setTimeout(showLogin, 1200);
  } catch (e) { $("pwErr").textContent = e.message; }
}

// ---------- Ereignisse ----------
$("loginBtn").onclick = login;
$("lp").addEventListener("keydown", e => e.key === "Enter" && login());
$("lo").addEventListener("keydown", e => e.key === "Enter" && login());
$("logoutBtn").onclick = async () => { try { await api("auth/logout", { method: "POST" }); } finally { session.set(""); showLogin(); } };
$("tabs").addEventListener("click", e => { const b = e.target.closest("button[data-tab]"); if (b) showTab(b.dataset.tab); });
$("clockBtn").onclick = clockAction;
$("mMonth").onchange = () => { if ($("mMonth").value) { state.month = $("mMonth").value; state.openDay = null; loadMonth(); } };
$("mPrev").onclick = () => shiftMonth(-1);
$("mNext").onclick = () => shiftMonth(1);
$("mTable").addEventListener("click", e => onMonthClick(e).catch(err => alert(err.message)));
$("csvBtn").onclick = async () => {
  const { from, to } = monthRange();
  $("mMsg").textContent = "";
  try { await downloadFile(`api/v1/reports/export.csv?user_id=${state.uid}&from=${from}&to=${to}`, "zeiterfassung.csv"); }
  catch (e) { $("mMsg").textContent = e.message; }
};
$$(".dl").forEach(b => b.onclick = () => downloadReport(b));
$$(".mailRep").forEach(b => b.onclick = () => mailReport(b));
$("mailSave").onclick = saveMail;
$("smtpSec").onchange = () => {  // passenden Standard-Port vorschlagen
  const port = { starttls: "587", ssl: "465", none: "25" }[$("smtpSec").value];
  if (["587", "465", "25", ""].includes($("smtpPort").value)) $("smtpPort").value = port;
};
$("mailTest").onclick = testMail;
$("meMailSave").onclick = saveMeMail;
$("yPrev").onclick = () => { state.year--; clearSel(); loadYear(); };
$("yNext").onclick = () => { state.year++; clearSel(); loadYear(); };
$$(".userSel").forEach(s => s.addEventListener("change", () => {
  state.uid = Number(s.value); state.openDay = null; clearSel();
  $$(".userSel").forEach(o => { o.value = s.value; });
  if (state.tab === "month") loadMonth(); else loadYear();
}));
$("usersTable").addEventListener("click", e => {
  const b = e.target.closest(".usave"); if (b) return saveUserRow(b);
  const v = e.target.closest(".usched"); if (v) return toggleSchedules(v.closest("tr"));
  const a = e.target.closest("[data-sact]"); if (a) return scheduleAction(a);
});
$("newUserBtn").onclick = newUser;
$("setBreak").onchange = () => $("setMinWrap").classList.toggle("hidden", $("setBreak").value !== "fixed");
$("setBtn").onclick = saveSettings;
$("impPreview").onclick = () => runImport(false);
$("impCommit").onclick = () => runImport(true);
$("impFile").onchange = () => $("impResult").classList.add("hidden");
$("impKind").onchange = () => $("impResult").classList.add("hidden");
$("qrBtn").onclick = () => makeQr().catch(e => alert(e.message));
$("qrUser").addEventListener("change", () => { $("qrBox").classList.add("hidden"); loadTokens().catch(() => {}); });
$("pwBtn").onclick = changePw;
setupYearSelection();

start().catch(showLogin);
