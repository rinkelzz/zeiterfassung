/**
 * Zeiterfassung – natives DSM-Fenster (DSM 7.x, Ext JS 3).
 *
 * Ab DSM 7.2 öffnet DSM Drittanbieter-Apps vom Typ "url"/"legacy" nur noch in
 * einem neuen Tab. Damit die Zeiterfassung wie das Paket-Zentrum in einem
 * eigenen DSM-Fenster läuft, registrieren wir eine echte DSM-App und zeigen die
 * Weboberfläche darin per iframe an – über app.cgi, also über den Webserver von
 * DSM selbst. So blockiert DSM den Inhalt nicht, und app.cgi kann den
 * angemeldeten DSM-Benutzer an die Zeiterfassung weitergeben (Single Sign-on).
 */
Ext.ns("SYNO.SDS.Zeiterfassung");

Ext.define("SYNO.SDS.Zeiterfassung.Application", {
    extend: "SYNO.SDS.AppInstance",
    appWindowName: "SYNO.SDS.Zeiterfassung.AppWindow",
    constructor: function () {
        this.callParent(arguments);
    }
});

Ext.define("SYNO.SDS.Zeiterfassung.AppWindow", {
    extend: "SYNO.SDS.AppWindow",

    // Weiterleitung über den Webserver von DSM → gleicher Ursprung, nicht blockiert, Single Sign-on
    START_URL: "/webman/3rdparty/zeiterfassung/app.cgi",

    synoToken: function () {
        try {
            if (SYNO.SDS.Session && SYNO.SDS.Session.SynoToken) { return SYNO.SDS.Session.SynoToken; }
        } catch (e) { /* ältere DSM */ }
        try { if (typeof _S === "function" && _S("SynoToken")) { return _S("SynoToken"); } } catch (e2) { /* egal */ }
        return "";
    },

    constructor: function (cfg) {
        var me = this;
        me.frameId = "zeiterfassung-frame-" + Ext.id();
        cfg = Ext.apply({
            title: "Zeiterfassung",
            width: 1120,
            height: 760,
            minWidth: 420,
            minHeight: 420,
            resizable: true,
            maximizable: true,
            minimizable: true,
            layout: "fit",
            items: [{
                xtype: "box",
                autoEl: {
                    tag: "iframe",
                    id: me.frameId,
                    src: me.START_URL + "?SynoToken=" + encodeURIComponent(me.synoToken()) +
                         "&t=" + new Date().getTime() + "&p=%2F",
                    frameborder: 0,
                    allow: "clipboard-write",
                    style: "width:100%;height:100%;border:0;display:block;background:#f4f5f7"
                }
            }]
        }, cfg);
        me.callParent([cfg]);
    },

    onOpen: function () {
        var me = this;
        // Klicks im iframe erreichen DSM nicht – deshalb das Fenster nach vorne holen,
        // sobald der Fokus in unseren iframe wandert.
        me._onBlur = function () {
            setTimeout(function () {
                var f = document.getElementById(me.frameId);
                if (f && document.activeElement === f && me.toFront) {
                    me.toFront();
                }
            }, 0);
        };
        window.addEventListener("blur", me._onBlur);
        me.callParent(arguments);
    },

    onRequest: function () {
        this.callParent(arguments);
    },

    onClose: function () {
        if (this._onBlur) {
            window.removeEventListener("blur", this._onBlur);
        }
        return this.callParent(arguments);
    }
});
