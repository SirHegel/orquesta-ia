import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


SHELL = pathlib.Path(__file__).resolve().parents[1] / "shell.sh"
PROYECTO = SHELL.parent
ARCHIVOS_INSTALACION = (
    "orq", "orqchat.py", "orqlib.py", "orqroot.py", "orqenv.py", "orqlogin.py",
    "orqrun.py",
    "shell.sh", "tools/minimax",
)


class ShellPortabilityTests(unittest.TestCase):
    def copiar_instalacion(self, raiz, perfiles):
        raiz.mkdir(parents=True)
        for relativa in ARCHIVOS_INSTALACION:
            origen = PROYECTO / relativa
            destino = raiz / relativa
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origen, destino)
        (raiz / "profiles.json").write_text(
            json.dumps(perfiles), encoding="utf-8"
        )
        (raiz / "profiles.json").chmod(0o600)

    def ejecutar(self, perfiles, script):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = pathlib.Path(tmp) / "Orquesta Con Espacio"
            self.copiar_instalacion(raiz, {"profiles": perfiles})
            # Los perfiles MiniMax de estas pruebas representan cuentas ya
            # conectadas. La política productiva solo acepta el archivo
            # dedicado ``<home>/api_key``.
            for pid, perfil in perfiles.items():
                if perfil.get("provider") != "minimax":
                    continue
                clave = raiz / "accounts" / pid / "api_key"
                clave.parent.mkdir(parents=True, exist_ok=True)
                clave.write_text("minimax-test-key", encoding="utf-8")
                clave.chmod(0o600)
            config = pathlib.Path(tmp) / "config-vacio"
            config.mkdir()
            env = dict(os.environ)
            for nombre in (
                "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
                "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_AUTH_TOKEN",
                "ORQ_CUENTA",
            ):
                env.pop(nombre, None)
            env.update({
                "ORQ_HOME": str(raiz), "ORQ_AUTO_CHAT": "0",
                "XDG_CONFIG_HOME": str(config), "TERM": "xterm-256color",
            })
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "$ORQ_HOME/shell.sh"; {script}'],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            return proceso.stdout, raiz

    def test_home_administrado_y_ruta_del_clon_admiten_espacios(self):
        salida, raiz = self.ejecutar(
            {"claude-prueba": {
                "provider": "claude", "home": "accounts/claude-prueba"
            }},
            'orquse claude-prueba >/dev/null; printf "RESULT=%s\\n" "$CLAUDE_CONFIG_DIR"',
        )
        self.assertIn(f"RESULT={raiz / 'accounts' / 'claude-prueba'}", salida)

    def test_cambiar_minimax_a_claude_limpia_endpoint_y_modelo(self):
        salida, _ = self.ejecutar(
            {
                "minimax": {
                    "provider": "minimax", "home": "accounts/minimax",
                    "base_url": "https://api.minimax.io/anthropic",
                    "model": "mm-test",
                },
                "claude-prueba": {
                    "provider": "claude", "home": "accounts/claude-prueba",
                },
            },
            "orquse minimax >/dev/null; orquse claude-prueba >/dev/null; "
            'printf "RESULT=%s|%s\\n" "${ANTHROPIC_BASE_URL-unset}" '
            '"${ANTHROPIC_MODEL-unset}"',
        )
        self.assertIn("RESULT=unset|unset", salida)

    def test_shell_root_a_gana_a_un_cli_enlazado_a_root_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz_a = base / "Instalacion A con espacios"
            raiz_b = base / "Instalacion B con espacios"
            self.copiar_instalacion(raiz_a, {"profiles": {
                "cuenta-a": {"provider": "claude", "enabled": False},
            }})
            self.copiar_instalacion(raiz_b, {"profiles": {
                "cuenta-b": {"provider": "claude", "enabled": False},
            }})
            binario = base / "bin externo" / "orq"
            binario.parent.mkdir()
            binario.symlink_to(raiz_b / "orq")

            env = dict(os.environ)
            # Reproduce una terminal que conservaba la copia B: cargar
            # explícitamente shell.sh de A debe reemplazar ese valor obsoleto.
            env["ORQ_HOME"] = str(raiz_b)
            config = base / "config"
            config.mkdir()
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color",
                "PATH": f"{binario.parent}:{env['PATH']}",
            })
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; orq cuentas'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn("cuenta-a", proceso.stdout)
            self.assertNotIn("cuenta-b", proceso.stdout)

    def test_export_posterior_no_cambia_a_pero_source_b_si(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz_a = base / "Instalacion A"
            raiz_b = base / "Instalacion B"
            self.copiar_instalacion(raiz_a, {"profiles": {
                "cuenta-a": {"provider": "claude", "enabled": False},
            }})
            self.copiar_instalacion(raiz_b, {"profiles": {
                "cuenta-b": {"provider": "claude", "enabled": False},
            }})
            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color",
            })

            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; '
                 f'export ORQ_HOME="{raiz_b}"; '
                 'printf "INICIO-A\\n"; orq cuentas; '
                 'printf "HOME-A=%s\\n" "$ORQ_HOME"; '
                 f'. "{raiz_b / "shell.sh"}"; '
                 'printf "INICIO-B\\n"; orq cuentas; '
                 'printf "HOME-B=%s\\n" "$ORQ_HOME"'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            salida_a, salida_b = proceso.stdout.split("INICIO-B", 1)
            self.assertIn("cuenta-a", salida_a)
            self.assertNotIn("cuenta-b", salida_a)
            self.assertIn(f"HOME-A={raiz_a}", salida_a)
            self.assertIn("cuenta-b", salida_b)
            self.assertNotIn("cuenta-a", salida_b)
            self.assertIn(f"HOME-B={raiz_b}", salida_b)

    def test_cambiar_raiz_limpia_estado_aunque_el_mtime_coincida(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz_a = base / "Instalacion A"
            raiz_b = base / "Instalacion B"
            perfiles_a = {
                "profiles": {
                    "gpt-a": {
                        "provider": "gpt", "home": "accounts/gpt-a",
                    },
                    "claude-a": {
                        "provider": "claude", "home": "accounts/claude-a",
                    },
                },
                "_activas": {"gpt": "gpt-a", "claude": "claude-a"},
            }
            perfiles_b = {
                "profiles": {
                    "claude-b": {
                        "provider": "claude", "home": "accounts/claude-b",
                    },
                },
                "_activas": {"claude": "claude-b"},
            }
            self.copiar_instalacion(raiz_a, perfiles_a)
            self.copiar_instalacion(raiz_b, perfiles_b)
            credenciales = (
                raiz_a / "accounts" / "gpt-a" / "auth.json",
                raiz_a / "accounts" / "claude-a" / ".credentials.json",
                raiz_b / "accounts" / "claude-b" / ".credentials.json",
            )
            for credencial in credenciales:
                credencial.parent.mkdir(parents=True, exist_ok=True)
                credencial.write_text("{}", encoding="utf-8")
                credencial.chmod(0o600)
            estado_a = raiz_a / "state" / "entorno.sh"
            estado_b = raiz_b / "state" / "entorno.sh"
            estado_a.parent.mkdir()
            estado_b.parent.mkdir()
            estado_a.write_text("orquesta-entorno-v2\n", encoding="utf-8")
            # B omite GPT deliberadamente: sus variables de A deben desaparecer.
            estado_b.write_text("orquesta-entorno-v2\n", encoding="utf-8")
            mismo_mtime = 1_700_000_000
            os.utime(estado_a, (mismo_mtime, mismo_mtime))
            os.utime(estado_b, (mismo_mtime, mismo_mtime))
            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            for nombre in (
                "CODEX_HOME", "CLAUDE_CONFIG_DIR", "ORQ_GPT_CUENTA",
                "ORQ_CLAUDE_CUENTA", "ORQ_CUENTA", "ORQ_PERMISOS_TOTALES",
            ):
                env.pop(nombre, None)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color",
            })

            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; ORQ_CUENTA=manual-a; '
                 'ORQ_PERMISOS_TOTALES=1; '
                 f'. "{raiz_b / "shell.sh"}"; '
                 'printf "RESULT=%s|%s|%s|%s|%s|%s|%s\\n" '
                 '"$ORQ_HOME" "${CODEX_HOME-unset}" '
                 '"${ORQ_GPT_CUENTA-unset}" "$CLAUDE_CONFIG_DIR" '
                 '"$ORQ_CLAUDE_CUENTA" "${ORQ_PERMISOS_TOTALES-unset}" '
                 '"${ORQ_CUENTA-unset}"'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn(
                f"RESULT={raiz_b}|unset|unset|"
                f"{raiz_b / 'accounts/claude-b'}|claude-b|unset|unset",
                proceso.stdout,
            )

    def test_config_b_desactiva_wrappers_peligrosos_de_a(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz_a = base / "Instalacion A"
            raiz_b = base / "Instalacion B"
            self.copiar_instalacion(raiz_a, {"profiles": {}})
            self.copiar_instalacion(raiz_b, {"profiles": {}})
            config_a = base / "config-a" / "orquesta"
            config_b = base / "config-b" / "orquesta"
            config_a.mkdir(parents=True)
            config_b.mkdir(parents=True)
            (config_a / "shell.local.sh").write_text(
                "ORQ_AUTO_CHAT=0\nORQ_PERMISOS_TOTALES=1\n",
                encoding="utf-8",
            )
            (config_b / "shell.local.sh").write_text(
                "ORQ_AUTO_CHAT=0\nORQ_PERMISOS_TOTALES=0\n",
                encoding="utf-8",
            )
            (config_a / "shell.local.sh").chmod(0o600)
            (config_b / "shell.local.sh").chmod(0o600)
            home = base / "home"
            home.mkdir()
            binarios = base / "bin-falso"
            binarios.mkdir()
            codex = binarios / "codex"
            codex.write_text(
                '#!/bin/sh\nprintf "CODEX=%s\\n" "$*"\n',
                encoding="utf-8",
            )
            codex.chmod(0o755)
            env = dict(os.environ)
            for nombre in (
                "ORQ_AUTO_CHAT", "ORQ_PERMISOS_TOTALES", "ORQ_HOME",
            ):
                env.pop(nombre, None)
            env.update({
                "HOME": str(home), "TERM": "xterm-256color",
                "PATH": f"{binarios}:{env['PATH']}",
                "XDG_CONFIG_HOME": str(config_a.parent),
            })

            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; '
                 'printf "INICIO-A|%s\\n" "$ORQ_PERMISOS_TOTALES"; '
                 'codex prueba; '
                 f'XDG_CONFIG_HOME="{config_b.parent}"; '
                 f'. "{raiz_b / "shell.sh"}"; '
                 'printf "INICIO-B|%s|%s\\n" '
                 '"$ORQ_PERMISOS_TOTALES" "$ORQ_HOME"; codex prueba'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            salida_a, salida_b = proceso.stdout.split("INICIO-B", 1)
            self.assertIn("INICIO-A|1", salida_a)
            self.assertIn(
                "CODEX=--dangerously-bypass-approvals-and-sandbox prueba",
                salida_a,
            )
            self.assertIn(f"|0|{raiz_b}", salida_b)
            self.assertIn("CODEX=prueba", salida_b)
            self.assertNotIn("dangerously-bypass", salida_b)

    def test_shell_root_a_gana_al_minimax_enlazado_a_root_b(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz_a = base / "Minimax A con espacios"
            raiz_b = base / "Minimax B con espacios"
            key_a = raiz_a / "accounts" / "mm-a" / "api_key"
            key_b = raiz_b / "accounts" / "mm-b" / "api_key"
            perfiles_a = {
                "profiles": {"mm-a": {
                    "provider": "minimax", "home": "accounts/mm-a",
                    "api_key_file": "api_key",
                }},
                "_activas": {"minimax": "mm-a"},
            }
            perfiles_b = {
                "profiles": {"mm-b": {
                    "provider": "minimax", "home": "accounts/mm-b",
                    "api_key_file": "api_key",
                }},
                "_activas": {"minimax": "mm-b"},
            }
            self.copiar_instalacion(raiz_a, perfiles_a)
            self.copiar_instalacion(raiz_b, perfiles_b)
            for ruta in (key_a, key_b):
                ruta.parent.mkdir(parents=True)
                ruta.write_text("valor-prueba", encoding="utf-8")
                ruta.chmod(0o600)

            bin_dir = base / "bin falso con espacios"
            bin_dir.mkdir()
            minimax = bin_dir / "minimax"
            minimax.symlink_to(raiz_b / "tools" / "minimax")
            claude = bin_dir / "claude"
            claude.write_text(
                '#!/bin/sh\nprintf "RESULT=%s|%s\\n" "$ORQ_HOME" '
                '"$CLAUDE_CONFIG_DIR"\n',
                encoding="utf-8",
            )
            claude.chmod(0o755)

            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            env.pop("ORQ_HOME", None)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color", "PATH": f"{bin_dir}:{env['PATH']}",
            })
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; minimax --cuenta mm-b -p prueba'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            # A no conoce mm-b; llegar a este error demuestra que la función
            # fijada por shell.sh ejecutó el wrapper de A y no el symlink de B.
            self.assertNotEqual(proceso.returncode, 0)
            self.assertIn("cuenta MiniMax desconocida: mm-b", proceso.stderr)
            self.assertNotIn("RESULT=", proceso.stdout)

    def test_orq_home_invalido_falla_sin_usar_la_copia_del_binario(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["ORQ_HOME"] = str(pathlib.Path(tmp) / "no-existe")
            proceso = subprocess.run(
                [str(PROYECTO / "orq"), "--help"],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertNotEqual(proceso.returncode, 0)
            self.assertIn("ORQ_HOME no coincide", proceso.stderr)

    def test_binario_b_explicito_falla_dentro_del_shell_de_a(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz_a = base / "A"
            raiz_b = base / "B"
            self.copiar_instalacion(raiz_a, {"profiles": {}})
            self.copiar_instalacion(raiz_b, {"profiles": {}})
            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            env.update({
                "ORQ_HOME": str(raiz_b), "ORQ_AUTO_CHAT": "0",
                "XDG_CONFIG_HOME": str(config), "TERM": "xterm-256color",
            })

            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; "{raiz_b / "orq"}" --help'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertNotEqual(proceso.returncode, 0)
            self.assertIn("ORQ_HOME no coincide", proceso.stderr)

    def test_shell_local_no_puede_cambiar_la_raiz_validada(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz_a = base / "A con espacios"
            raiz_b = base / "B con espacios"
            self.copiar_instalacion(raiz_a, {"profiles": {}})
            self.copiar_instalacion(raiz_b, {"profiles": {}})
            config = base / "config" / "orquesta"
            config.mkdir(parents=True)
            (config / "shell.local.sh").write_text(
                f'ORQ_HOME="{raiz_b}"\nORQ_PRUEBA_LOCAL=conservada\n'
                'ORQ_AUTO_CHAT=0\n',
                encoding="utf-8",
            )
            (config / "shell.local.sh").chmod(0o600)
            env = dict(os.environ)
            env.update({
                "ORQ_HOME": str(raiz_b),
                "XDG_CONFIG_HOME": str(config.parent),
                "TERM": "xterm-256color",
            })

            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; '
                 'printf "RESULT=%s|%s\\n" "$ORQ_HOME" "$ORQ_PRUEBA_LOCAL"'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn(f"RESULT={raiz_a}|conservada", proceso.stdout)

    def test_raiz_completa_puede_llegar_mediante_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz = base / "Instalacion real"
            alias = base / "Alias de instalacion"
            self.copiar_instalacion(raiz, {"profiles": {}})
            alias.symlink_to(raiz, target_is_directory=True)
            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            env.update({
                "ORQ_HOME": str(base / "valor-obsoleto"),
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color",
            })

            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{alias / "shell.sh"}"; printf "RESULT=%s\\n" "$ORQ_HOME"'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn(f"RESULT={raiz}", proceso.stdout)

    def test_orqlib_symlink_interno_se_rechaza_antes_de_importarlo(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = pathlib.Path(tmp) / "A"
            self.copiar_instalacion(raiz, {"profiles": {}})
            destino = raiz / "lib" / "orqlib.py"
            destino.parent.mkdir()
            shutil.move(raiz / "orqlib.py", destino)
            (raiz / "orqlib.py").symlink_to(destino)
            destino.write_text(
                'raise RuntimeError("orqlib enlazado fue importado")\n',
                encoding="utf-8",
            )
            env = dict(os.environ)
            env["ORQ_HOME"] = str(raiz)

            proceso = subprocess.run(
                [str(raiz / "orq"), "--help"], env=env,
                capture_output=True, text=True, timeout=15,
            )

            self.assertNotEqual(proceso.returncode, 0)
            self.assertIn(
                "orqlib.py debe ser un archivo regular exacto", proceso.stderr
            )
            self.assertNotIn("orqlib enlazado fue importado", proceso.stderr)

    def test_orq_symlink_interno_se_rechaza_al_cargar_el_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz = base / "A"
            self.copiar_instalacion(raiz, {"profiles": {}})
            destino = raiz / "bin" / "orq-real"
            destino.parent.mkdir()
            shutil.move(raiz / "orq", destino)
            (raiz / "orq").symlink_to(destino)
            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color",
            })

            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz / "shell.sh"}"'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertNotEqual(proceso.returncode, 0)
            self.assertIn("orq debe ser un archivo regular exacto", proceso.stderr)

    def test_shell_local_es_trust_anchor_pero_rechaza_modo_escribible(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz = base / "Instalacion"
            self.copiar_instalacion(raiz, {"profiles": {}})
            config = base / "config" / "orquesta"
            config.mkdir(parents=True)
            local = config / "shell.local.sh"
            local.write_text("ORQ_INYECTADA=si\nORQ_AUTO_CHAT=1\n", encoding="utf-8")
            local.chmod(0o664)
            env = dict(os.environ)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config.parent),
                "TERM": "xterm-256color",
            })
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz / "shell.sh"}"; '
                 'printf "RESULT=%s\\n" "${ORQ_INYECTADA-unset}"'],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn("RESULT=unset", proceso.stdout)
            self.assertIn("shell.local.sh inseguro", proceso.stderr)

    def test_shell_local_fifo_falla_rapido_y_no_evalua_salida(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz = base / "Instalacion"
            self.copiar_instalacion(raiz, {"profiles": {}})
            config = base / "config" / "orquesta"
            config.mkdir(parents=True)
            local = config / "shell.local.sh"
            os.mkfifo(local, 0o600)
            marca = base / "config-ejecutada"
            env = dict(os.environ)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config.parent),
                "TERM": "xterm-256color",
            })

            inicio = __import__("time").monotonic()
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz / "shell.sh"}"; '
                 f'test ! -e "{marca}"; printf "RESULT=ok\\n"'],
                env=env, capture_output=True, text=True, timeout=5,
            )

            self.assertLess(__import__("time").monotonic() - inicio, 4)
            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn("RESULT=ok", proceso.stdout)
            self.assertFalse(marca.exists())
            self.assertIn("shell.local.sh inseguro", proceso.stderr)

    def test_path_heredado_no_suplanta_utilidades_del_arranque(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz = base / "Instalacion"
            self.copiar_instalacion(raiz, {"profiles": {}})
            estado = raiz / "state" / "entorno.sh"
            estado.parent.mkdir()
            estado.write_text("orquesta-entorno-v2\n", encoding="utf-8")
            falsos = base / "bin-falso"
            falsos.mkdir()
            marca = base / "utilidad-ejecutada"
            for nombre in ("python3", "date", "ps", "stat", "sed", "tr"):
                ruta = falsos / nombre
                ruta.write_text(
                    f"#!/bin/sh\ntouch '{marca}'\nexit 99\n", encoding="utf-8"
                )
                ruta.chmod(0o755)
            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            env.pop("ORQ_SESION", None)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color", "PATH": f"{falsos}:/usr/bin:/bin",
            })
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz / "shell.sh"}"; printf "RESULT=ok\\n"'],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn("RESULT=ok", proceso.stdout)
            self.assertFalse(marca.exists())

    def test_entrypoints_reales_no_usan_python_del_path_heredado(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz = base / "Instalacion"
            self.copiar_instalacion(raiz, {"profiles": {}})
            falsos = base / "bin-falso"
            falsos.mkdir()
            marca = base / "python-falso-ejecutado"
            python_falso = falsos / "python3"
            python_falso.write_text(
                f"#!/bin/sh\ntouch '{marca}'\nexit 97\n", encoding="utf-8"
            )
            python_falso.chmod(0o755)
            config = base / "config-vacio"
            config.mkdir()
            env = dict(os.environ)
            env.update({
                "ORQ_HOME": str(raiz), "ORQ_AUTO_CHAT": "0",
                "XDG_CONFIG_HOME": str(config), "TERM": "xterm-256color",
                "PATH": f"{falsos}:/usr/bin:/bin",
            })

            cli = subprocess.run(
                [str(raiz / "orq"), "--help"], env=env,
                capture_output=True, text=True, timeout=10,
            )
            chat = subprocess.run(
                [str(raiz / "orqchat.py")], env=env, input="/salir\n",
                capture_output=True, text=True, timeout=10,
            )

            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(chat.returncode, 0, chat.stderr)
            self.assertFalse(marca.exists())

    def test_cuentas_activas_eliminan_credenciales_heredadas_conflictivas(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            raiz = base / "Instalacion"
            perfiles = {
                "profiles": {
                    "claude-a": {
                        "provider": "claude", "home": "accounts/claude-a",
                    },
                    "gpt-a": {"provider": "gpt", "home": "accounts/gpt-a"},
                },
                "_activas": {"claude": "claude-a", "gpt": "gpt-a"},
            }
            self.copiar_instalacion(raiz, perfiles)
            credenciales = (
                raiz / "accounts" / "claude-a" / ".credentials.json",
                raiz / "accounts" / "gpt-a" / "auth.json",
            )
            for credencial in credenciales:
                credencial.parent.mkdir(parents=True, exist_ok=True)
                credencial.write_text("{}", encoding="utf-8")
                credencial.chmod(0o600)
            estado = raiz / "state" / "entorno.sh"
            estado.parent.mkdir()
            estado.write_text("orquesta-entorno-v2\n", encoding="utf-8")
            config = base / "config"
            config.mkdir()
            env = dict(os.environ)
            env.update({
                "ORQ_AUTO_CHAT": "0", "XDG_CONFIG_HOME": str(config),
                "TERM": "xterm-256color", "ANTHROPIC_API_KEY": "no-heredar",
                "OPENAI_API_KEY": "no-heredar", "GEMINI_API_KEY": "conservar",
            })
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz / "shell.sh"}"; '
                 'printf "RESULT=%s|%s|%s|%s|%s\\n" '
                 '"${ANTHROPIC_API_KEY-unset}" "${OPENAI_API_KEY-unset}" '
                 '"$GEMINI_API_KEY" "$ORQ_CLAUDE_CUENTA" "$ORQ_GPT_CUENTA"'],
                env=env, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn("RESULT=unset|unset|conservar|claude-a|gpt-a", proceso.stdout)


if __name__ == "__main__":
    unittest.main()
