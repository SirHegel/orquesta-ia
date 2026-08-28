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
            })
            proceso = subprocess.run(
                ["bash", "--noprofile", "--norc", "-ic",
                 f'. "{raiz_a / "shell.sh"}"; "{binario}" cuentas'],
                env=env, capture_output=True, text=True, timeout=15,
            )

            self.assertEqual(proceso.returncode, 0, proceso.stderr)
            self.assertIn("cuenta-a", proceso.stdout)
            self.assertNotIn("cuenta-b", proceso.stdout)

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
                 f'. "{raiz_a / "shell.sh"}"; "{minimax}" -p prueba'],
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
            self.assertIn("ORQ_HOME no es un directorio", proceso.stderr)


if __name__ == "__main__":
    unittest.main()
