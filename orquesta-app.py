#!/usr/bin/env python3
"""Aplicativo de escritorio de Orquesta IA (GTK4 + WebKit6).

Levanta el panel si no está corriendo y lo muestra en una ventana nativa.
"""
import gi, os, socket, subprocess, sys, time
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, WebKit, GLib, Adw, Gio

PUERTO = int(os.environ.get("ORQ_PUERTO", "8787"))
URL = f"http://127.0.0.1:{PUERTO}"
BASE = os.path.dirname(os.path.abspath(__file__))


def puerto_vivo(p=PUERTO, t=0.35):
    with socket.socket() as s:
        s.settimeout(t)
        return s.connect_ex(("127.0.0.1", p)) == 0


def asegurar_panel():
    """Arranca el panel si hace falta. Devuelve (ok, mensaje)."""
    if puerto_vivo():
        return True, "panel ya en marcha"
    # 1) intentar via systemd (modo permanente)
    try:
        subprocess.run(["systemctl", "--user", "start", "orquesta.service"],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    for _ in range(20):
        if puerto_vivo():
            return True, "panel iniciado por systemd"
        time.sleep(0.25)
    # 2) fallback: lanzarlo suelto
    try:
        subprocess.Popen([sys.executable, os.path.join(BASE, "orqweb.py"),
                          str(PUERTO), "--no-abrir"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
        return False, f"no pude arrancar el panel: {e}"
    for _ in range(24):
        if puerto_vivo():
            return True, "panel iniciado en modo suelto"
        time.sleep(0.25)
    return False, f"el panel no respondió en {URL}"


class Ventana(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Orquesta IA")
        self.set_default_size(1180, 820)

        raiz = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cab = Adw.HeaderBar()
        titulo = Adw.WindowTitle(title="Orquesta IA", subtitle=URL)
        cab.set_title_widget(titulo)
        self.titulo = titulo

        b_rec = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Recargar (Ctrl+R)")
        b_rec.connect("clicked", lambda *_: self.web.reload())
        cab.pack_start(b_rec)

        b_nav = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Abrir en el navegador")
        b_nav.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(URL, None))
        cab.pack_end(b_nav)

        b_term = Gtk.Button(icon_name="utilities-terminal-symbolic", tooltip_text="Abrir terminal con orq")
        b_term.connect("clicked", self.abrir_terminal)
        cab.pack_end(b_term)

        raiz.append(cab)

        self.web = WebKit.WebView()
        s = self.web.get_settings()
        s.set_enable_developer_extras(True)
        s.set_javascript_can_access_clipboard(True)
        self.web.set_vexpand(True)
        self.web.connect("load-changed", self.al_cargar)
        raiz.append(self.web)

        self.banner = Adw.Banner(revealed=False)
        raiz.append(self.banner)

        self.set_content(raiz)

        # atajos
        ctl = Gtk.ShortcutController()
        for acc, fn in (("<Control>r", lambda *_: self.web.reload()),
                        ("F5", lambda *_: self.web.reload()),
                        ("<Control>q", lambda *_: self.close())):
            ctl.add_shortcut(Gtk.Shortcut(
                trigger=Gtk.ShortcutTrigger.parse_string(acc),
                action=Gtk.CallbackAction.new(lambda *a, f=fn: (f(), True)[1])))
        self.add_controller(ctl)

        GLib.idle_add(self.arrancar)

    def arrancar(self):
        ok, msg = asegurar_panel()
        if ok:
            self.web.load_uri(URL)
        else:
            self.banner.set_title(msg)
            self.banner.set_revealed(True)
            self.web.load_html(
                f"<body style='background:#08080a;color:#d6d6d8;font-family:monospace;"
                f"padding:40px'><h2 style='color:#e0322e'>El panel no responde</h2>"
                f"<p>{GLib.markup_escape_text(msg)}</p>"
                f"<p>Prueba desde una terminal:</p>"
                f"<pre style='background:#16161c;padding:12px;border-radius:8px'>"
                f"systemctl --user status orquesta.service\norq web</pre></body>", None)
        return False

    def al_cargar(self, web, evento):
        if evento == WebKit.LoadEvent.FINISHED:
            self.banner.set_revealed(False)

    def abrir_terminal(self, *_):
        for term, args in (("kitty", ["kitty", "--title", "orq", "-e", "bash", "-lc",
                                      "orq status; exec bash"]),
                           ("ptyxis", ["ptyxis", "--", "bash", "-lc", "orq status; exec bash"]),
                           ("gnome-terminal", ["gnome-terminal", "--", "bash", "-lc",
                                               "orq status; exec bash"])):
            if GLib.find_program_in_path(term):
                subprocess.Popen(args, start_new_session=True)
                return


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.orquesta.Panel")

    def do_activate(self):
        w = self.props.active_window or Ventana(self)
        w.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
