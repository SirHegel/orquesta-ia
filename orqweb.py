#!/usr/bin/env python3
"""Panel de control local del orquestador multi-cuenta. Solo 127.0.0.1."""
import json, os, sys, uuid, threading, webbrowser, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import orqlib as L

PUERTO = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
JOBS, JLOCK = {}, threading.Lock()
HTML_PATH = os.path.join(L.BASE, "web", "index.html")
HOSTS_VALIDOS = {f"127.0.0.1:{PUERTO}", f"localhost:{PUERTO}"}
ORIGENES_VALIDOS = {f"http://127.0.0.1:{PUERTO}", f"http://localhost:{PUERTO}"}
MAX_BODY = 2 * 1024 * 1024


# ---------------- estado ----------------
def estado(tarea="code"):
    cf = L.cfg().get("profiles", {})
    rows = L.ledger_rows()
    cuentas = []
    for pid, p in cf.items():
        b = L.bloqueado(pid)
        horas = p.get("ventana_horas") or L.VENTANA_PLAN.get(p.get("plan", "desconocido"), 5)
        cuentas.append({
            "id": pid, "label": p.get("label", pid), "provider": p.get("provider"),
            "plan": p.get("plan"), "proposito": p.get("proposito"),
            "enabled": p.get("enabled", True), "auth": L.autenticado(pid, p),
            "bloqueada": b.strftime("%H:%M") if b else None,
            "hoy": L.gastado_hoy(pid), "ventana": L.gastado_ventana(pid, horas),
            "cupo": p.get("cupo_ventana", 0), "budget": p.get("budget_tokens_dia", 0),
            "horas": horas, "model": p.get("model", ""),
            "navegador": p.get("navegador", ""), "auth_modo": p.get("auth", ""),
            "weights": p.get("weights", {}), "login": L.cmd_login(pid, p),
        })
    gasto = []
    for pid in sorted({r["perfil"] for r in rows}):
        rs = [r for r in rows if r["perfil"] == pid]
        tk = sum(r.get("tokens", 0) for r in rs)
        gasto.append({"pid": pid, "n": len(rs), "tokens": tk,
                      "por": tk // max(1, len(rs)),
                      "seg": round(sum(r.get("seg", 0) for r in rs) / max(1, len(rs)), 1)})
    sc = {}
    for pid, t in L.scores().items():
        sc[pid] = {k: round(v["suma"] / v["n"], 2) for k, v in t.items() if v.get("n")}
    return {"cuentas": cuentas, "gasto": gasto, "scores": sc, "tareas": L.TAREAS,
            "navegadores": L.navegadores(), "terminal": L.terminal_disponible()[0],
            "activas": L.activas(),
            "resumen_uso": L.resumen_uso(),
            "uso_semana": L.uso("semana", "perfil", 4),
            "uso_mes": L.uso("mes", "perfil", 3),
            "uso_sesion": L.uso("mes", "sesion", 1),
            "sesiones_activas": L.sesiones_externas(),
            "ranking": [{"pid": x["pid"], "pts": round(x["pts"], 2), "nota": x["nota"]}
                        for x in L.ranking(tarea)],
            "recientes": rows[-25:][::-1],
            "total_tokens": sum(r.get("tokens", 0) for r in rows), "total_llamadas": len(rows)}


# ---------------- trabajos asincronos ----------------
def _job_set(jid, **kw):
    with JLOCK:
        JOBS.setdefault(jid, {}).update(kw)


def ejecutar_job(jid, prompt, tarea, modo, perfil, timeout):
    try:
        disp = L.disponibles(tarea=tarea)
        if modo == "ask":
            if perfil:
                todos = L.disponibles(incluir_bloqueados=True, tarea=tarea)
                if perfil not in todos:
                    return _job_set(jid, estado="error",
                                    error=f"'{perfil}' no disponible para {tarea}")
                objetivo = {perfil: todos[perfil]}
            else:
                rk = L.ranking(tarea)
                if not rk:
                    return _job_set(jid, estado="error", error="ninguna cuenta disponible")
                objetivo = {rk[0]["pid"]: rk[0]["p"]}
        else:
            objetivo = dict(disp)
            if not objetivo:
                return _job_set(jid, estado="error", error="ninguna cuenta disponible")
            if modo == "audit":
                revisores = L.disponibles(tarea="review")
                if (not revisores or not any(
                        pid != rid for pid in objetivo for rid in revisores)):
                    return _job_set(
                        jid, estado="error",
                        error="se necesita otra cuenta compatible con review")
        _job_set(jid, estado="corriendo", fase="respuestas", cuentas=list(objetivo))
        with ThreadPoolExecutor(max_workers=len(objetivo)) as ex:
            res = list(ex.map(lambda kv: L.correr(kv[0], kv[1], prompt, tarea, timeout),
                              objetivo.items()))
        _job_set(jid, respuestas=res)
        if modo != "audit":
            return _job_set(jid, estado="listo", auditorias=[],
                            total=sum(r["tokens"] for r in res))
        _job_set(jid, fase="auditoria")
        trab = []
        for rid, rp in revisores.items():
            otros = [x for x in res if x["perfil"] != rid and x["texto"]]
            if not otros:
                continue
            bloques = "\n\n".join(f"### Respuesta de {o['perfil']}\n{o['texto'][:3000]}" for o in otros)
            pa = (f"Eres auditor tecnico. Pregunta original:\n{prompt}\n\n{bloques}\n\n"
                  "Para CADA respuesta ajena indica: (1) errores factuales concretos, "
                  "(2) omisiones importantes, (3) nota de 0 a 10. Breve y directo.")
            trab.append((rid, rp, pa))
        with ThreadPoolExecutor(max_workers=max(1, len(trab))) as ex:
            auds = list(ex.map(lambda t: L.correr(t[0], t[1], t[2], "review", timeout), trab))
        _job_set(jid, estado="listo", auditorias=auds,
                 total=sum(r["tokens"] for r in res + auds))
    except Exception as e:
        _job_set(jid, estado="error", error=f"{e}\n{traceback.format_exc()[-500:]}")


# ---------------- servidor ----------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _j(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def log_message(self, *a):
        pass

    def _host_local(self):
        return (self.headers.get("Host") or "").lower() in HOSTS_VALIDOS

    def do_GET(self):
        if not self._host_local():
            return self._j(403, {"error": "host no permitido"})
        u = urlparse(self.path)
        if u.path == "/api/state":
            t = (parse_qs(u.query).get("tarea") or ["code"])[0]
            return self._j(200, estado(t if t in L.TAREAS else "code"))
        if u.path.startswith("/api/job/"):
            jid = u.path.rsplit("/", 1)[-1]
            with JLOCK:
                j = JOBS.get(jid)
            return self._j(200, j) if j else self._j(404, {"error": "job desconocido"})
        if u.path in ("/", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html")
            except FileNotFoundError:
                return self._send(500, "falta web/index.html", "text/plain")
        self._j(404, {"error": "no encontrado"})

    def do_POST(self):
        if not self._host_local():
            return self._j(403, {"error": "host no permitido"})
        origen = self.headers.get("Origin")
        if origen and origen not in ORIGENES_VALIDOS:
            return self._j(403, {"error": "origen no permitido"})
        if (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower() != "application/json":
            return self._j(415, {"error": "se requiere application/json"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._j(413, {"error": "cuerpo vacio o demasiado grande"})
            d = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(d, dict):
                return self._j(400, {"error": "el JSON debe ser un objeto"})
        except Exception:
            return self._j(400, {"error": "json invalido"})
        try:
            return self._ruta(d)
        except Exception as e:
            return self._j(500, {"error": str(e)})

    def _ruta(self, d):
        p = urlparse(self.path).path
        if p == "/api/account":
            pid_raw = d.get("id")
            if not isinstance(pid_raw, str):
                return self._j(400, {"error": "id invalido"})
            pid = pid_raw.strip()
            if not L.id_perfil_valido(pid):
                return self._j(400, {"error": "id invalido"})
            prov = d.get("provider")
            if prov not in ("claude", "gpt", "antigravity"):
                return self._j(400, {"error": "proveedor invalido"})
            nav = d.get("navegador")
            if not L.navegador_valido(nav):
                return self._j(400, {"error": "navegador invalido"})
            with L.bloqueo():
                cf = L.cfg(); ps = cf.setdefault("profiles", {})
                if pid in ps:
                    return self._j(400, {"error": f"'{pid}' ya existe"})
                if (prov == "antigravity" and any(
                        x.get("provider") == "antigravity" for x in ps.values())):
                    return self._j(400, {"error": "Antigravity ya tiene un perfil global"})
                home = os.path.join(L.ACCOUNTS, pid)
                os.makedirs(home, exist_ok=True); os.chmod(home, 0o700)
                ps[pid] = {"label": d.get("label") or f"{prov} · {pid}", "provider": prov,
                           "home": home, "plan": d.get("plan", "pro"),
                           "ventana_horas": float(d.get("ventana") or 5),
                           "cupo_ventana": int(d.get("cupo") or 0),
                           "proposito": d.get("proposito", "general"), "enabled": True,
                           "navegador": nav or "",
                           "budget_tokens_dia": int(d.get("budget") or 0),
                           "weights": {t: 7 for t in L.TAREAS}}
                if prov == "antigravity":
                    ps[pid]["allowed_tasks"] = ["imagen"]
                    ps[pid]["power"] = {"imagen": 10}
                    ps[pid]["weights"] = {"imagen": 10}
                else:
                    ps[pid]["allowed_tasks"] = [t for t in L.TAREAS if t != "imagen"]
                    ps[pid]["power"] = 10
                L.guardar_cfg(cf)
            return self._j(200, {"ok": True, "login": L.cmd_login(pid, L.cfg()["profiles"][pid])})

        if p == "/api/profile":
            if not isinstance(d.get("id"), str) or not L.id_perfil_valido(d["id"]):
                return self._j(400, {"error": "id invalido"})
            if "navegador" in d and not L.navegador_valido(d.get("navegador")):
                return self._j(400, {"error": "navegador invalido"})
            with L.bloqueo():
                cf = L.cfg(); pr = cf.get("profiles", {}).get(d.get("id"))
                if not pr:
                    return self._j(404, {"error": "no existe"})
                for k, cast in (("label", str), ("plan", str), ("proposito", str),
                                ("model", str), ("navegador", str), ("ventana_horas", float),
                                ("cupo_ventana", int), ("budget_tokens_dia", int),
                                ("enabled", bool)):
                    if k in d:
                        try:
                            pr[k] = cast(d[k])
                        except (TypeError, ValueError):
                            return self._j(400, {"error": f"valor invalido en {k}"})
                if "weights" in d and isinstance(d["weights"], dict):
                    w = pr.setdefault("weights", {})
                    for t, v in d["weights"].items():
                        if t in L.TAREAS:
                            try:
                                w[t] = max(0, min(10, float(v)))
                            except (TypeError, ValueError):
                                pass
                L.guardar_cfg(cf)
            return self._j(200, {"ok": True})

        if p == "/api/delete":
            pid = d.get("id")
            if not isinstance(pid, str) or not L.id_perfil_valido(pid):
                return self._j(400, {"error": "id invalido"})
            with L.bloqueo():
                cf = L.cfg()
                pr = cf.get("profiles", {}).pop(pid, None)
                if not pr:
                    return self._j(404, {"error": "no existe"})
                L.guardar_cfg(cf)
            return self._j(200, {"ok": True, "home": L.home_de(pid, pr)})

        if p == "/api/limite":
            pid = d.get("id")
            if not isinstance(pid, str) or not L.id_perfil_valido(pid):
                return self._j(400, {"error": "id invalido"})
            L.limpiar_limite(pid)
            return self._j(200, {"ok": True})

        if p == "/api/score":
            rid = d.get("run_id", "")
            if not isinstance(rid, str):
                return self._j(400, {"error": "run_id invalido"})
            pid = rid.rsplit("-", 1)[0]
            if not L.id_perfil_valido(pid):
                return self._j(400, {"error": "run_id invalido"})
            tarea = d.get("tarea", "reasoning")
            try:
                nota = float(d.get("nota"))
            except (TypeError, ValueError):
                return self._j(400, {"error": "nota invalida"})
            if not (0 <= nota <= 10) or tarea not in L.TAREAS:
                return self._j(400, {"error": "nota 0-10 y tarea valida"})
            with L.bloqueo():
                s = L.scores(); s.setdefault(pid, {}).setdefault(tarea, {"suma": 0, "n": 0})
                s[pid][tarea]["suma"] += nota; s[pid][tarea]["n"] += 1
                L._escribir(L.SCORES, s)
            e = L.scores()[pid][tarea]
            return self._j(200, {"ok": True, "promedio": round(e["suma"] / e["n"], 2), "n": e["n"]})

        if p == "/api/usar":
            pid = d.get("id")
            if not isinstance(pid, str) or not L.id_perfil_valido(pid):
                return self._j(400, {"error": "id invalido"})
            ok, msg = L.usar(pid)
            return self._j(200 if ok else 400, {"ok": ok, "mensaje": msg})

        if p == "/api/login-launch":
            pid = d.get("id")
            if not isinstance(pid, str) or not L.id_perfil_valido(pid):
                return self._j(400, {"error": "id invalido"})
            pr = L.cfg().get("profiles", {}).get(pid)
            if not pr:
                return self._j(404, {"error": "no existe"})
            if d.get("navegador") is not None:
                if not L.navegador_valido(d.get("navegador")):
                    return self._j(400, {"error": "navegador invalido"})
                with L.bloqueo():
                    cf = L.cfg(); cf["profiles"][pid]["navegador"] = d["navegador"]
                    L.guardar_cfg(cf)
                pr = L.cfg()["profiles"][pid]
            ok, msg = L.lanzar_login(pid, pr)
            return self._j(200 if ok else 500,
                           {"ok": ok, "mensaje": msg, "comando": L.cmd_login(pid, pr)})

        if p == "/api/verificar":
            pid = d.get("id")
            if pid is not None and (not isinstance(pid, str) or not L.id_perfil_valido(pid)):
                return self._j(400, {"error": "id invalido"})
            ps = L.cfg().get("profiles", {})
            objetivo = ({pid: ps[pid]} if pid in ps else
                        {k: v for k, v in ps.items()
                         if v.get("enabled", True) and L.autenticado(k, v)})
            if not objetivo:
                return self._j(400, {"error": "nada que verificar"})
            jid = uuid.uuid4().hex[:12]
            _job_set(jid, estado="encolado", modo="verificar")

            def _ver():
                try:
                    _job_set(jid, estado="corriendo", fase="probando",
                             cuentas=list(objetivo))
                    with ThreadPoolExecutor(max_workers=len(objetivo)) as ex:
                        res = list(ex.map(
                            lambda kv: L.correr(
                                kv[0], kv[1], "di solo: ok",
                                "reasoning" if L.admite_tarea(kv[1], "reasoning") else "imagen",
                                90), objetivo.items()))
                    _job_set(jid, estado="listo", respuestas=res, auditorias=[],
                             total=sum(r["tokens"] for r in res))
                except Exception as e:
                    _job_set(jid, estado="error", error=str(e))
            threading.Thread(target=_ver, daemon=True).start()
            return self._j(200, {"job": jid})

        if p == "/api/run":
            if not isinstance(d.get("prompt", ""), str):
                return self._j(400, {"error": "prompt invalido"})
            prompt = (d.get("prompt") or "").strip()
            if not prompt:
                return self._j(400, {"error": "prompt vacio"})
            tarea = d.get("tarea", "reasoning")
            if tarea not in L.TAREAS:
                tarea = "reasoning"
            modo = d.get("modo", "ask")
            if modo not in ("ask", "fan", "audit"):
                modo = "ask"
            perfil_raw = d.get("perfil")
            if perfil_raw not in (None, "") and not isinstance(perfil_raw, str):
                return self._j(400, {"error": "perfil invalido"})
            perfil = perfil_raw or None
            try:
                timeout_raw = d.get("timeout", 300)
                if isinstance(timeout_raw, bool):
                    raise ValueError
                timeout = int(timeout_raw)
            except (TypeError, ValueError):
                return self._j(400, {"error": "timeout invalido"})
            timeout = max(1, min(600, timeout))
            jid = uuid.uuid4().hex[:12]
            _job_set(jid, estado="encolado", modo=modo, tarea=tarea, prompt=prompt[:300])
            threading.Thread(target=ejecutar_job, daemon=True, args=(
                jid, prompt, tarea, modo, perfil, timeout)).start()
            return self._j(200, {"job": jid})

        self._j(404, {"error": "no encontrado"})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PUERTO), H)
    url = f"http://127.0.0.1:{PUERTO}"
    print(f"Panel Orquesta IA -> {url}   (Ctrl-C para parar)")
    if "--no-abrir" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nparado")
