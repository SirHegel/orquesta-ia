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
    "orq", "orqlib.py", "orqroot.py", "shell.sh", "tools/minimax",
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

    def ejecutar(self, perfiles, script):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = pathlib.Path(tmp) / "Orquesta Con Espacio"
            self.copiar_instalacion(raiz, {"profiles": perfiles})
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

    def test_home_relativo_y_ruta_del_clon_admiten_espacios(self):
        salida, raiz = self.ejecutar(
            {"claude-prueba": {
                "provider": "claude", "home": "accounts/Claude con espacio"
            }},
            'orquse claude-prueba >/dev/null; printf "RESULT=%s\\n" "$CLAUDE_CONFIG_DIR"',
        )
        self.assertIn(f"RESULT={raiz / 'accounts' / 'Claude con espacio'}", salida)

    def test_cambiar_minimax_a_claude_limpia_endpoint_y_modelo(self):
        salida, _ = self.ejecutar(
            {
                "minimax": {
                    "provider": "minimax", "home": "accounts/minimax",
                    "base_url": "https://minimax.invalid", "model": "mm-test",
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
            self.copiar_instalacion(raiz_a, {"profiles": {}})
            self.copiar_instalacion(raiz_b, {"profiles": {}})
            estado_a = raiz_a / "state" / "entorno.sh"
            estado_b = raiz_b / "state" / "entorno.sh"
            estado_a.parent.mkdir()
            estado_b.parent.mkdir()
            estado_a.write_text(
                f'export CODEX_HOME="{raiz_a / "accounts/gpt-a"}"\n'
                f'export CLAUDE_CONFIG_DIR="{raiz_a / "accounts/claude-a"}"\n'
                'export ORQ_GPT_CUENTA=cuenta-gpt-a\n'
                'export ORQ_CLAUDE_CUENTA=cuenta-claude-a\n'
                'export ORQ_PERMISOS_TOTALES=1\n',
                encoding="utf-8",
            )
            # B omite GPT deliberadamente: sus variables de A deben desaparecer.
            estado_b.write_text(
                f'export CLAUDE_CONFIG_DIR="{raiz_b / "accounts/claude-b"}"\n'
                'export ORQ_CLAUDE_CUENTA=cuenta-claude-b\n'
                'export ORQ_PERMISOS_TOTALES=0\n',
                encoding="utf-8",
            )
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
                f"{raiz_b / 'accounts/claude-b'}|cuenta-claude-b|unset|unset",
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
            key_a = raiz_a / "accounts" / "mm-a" / "key"
            key_b = raiz_b / "accounts" / "mm-b" / "key"
            perfiles_a = {
                "profiles": {"mm-a": {
                    "provider": "minimax", "home": "accounts/mm-a",
                    "api_key_file": str(key_a),
                }},
                "_activas": {"minimax": "mm-a"},
            }
            perfiles_b = {
                "profiles": {"mm-b": {
                    "provider": "minimax", "home": "accounts/mm-b",
                    "api_key_file": str(key_b),
                }},
                "_activas": {"minimax": "mm-b"},
            }
            self.copiar_instalacion(raiz_a, perfiles_a)
            self.copiar_instalacion(raiz_b, perfiles_b)
            for ruta in (key_a, key_b):
                ruta.parent.mkdir(parents=True)
                ruta.write_text("valor-prueba", encoding="utf-8")

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
                 f'. "{raiz_a / "shell.sh"}"; minimax -p prueba'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn(
                f"RESULT={raiz_a}|{raiz_a / 'accounts' / 'mm-a'}",
                proceso.stdout,
            )
            self.assertNotIn(
                str(raiz_b / "accounts" / "mm-b"), proceso.stdout
            )

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


if __name__ == "__main__":
    unittest.main()
