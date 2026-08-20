import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


SHELL = pathlib.Path(__file__).resolve().parents[1] / "shell.sh"


class ShellPortabilityTests(unittest.TestCase):
    def ejecutar(self, perfiles, script):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = pathlib.Path(tmp) / "Orquesta Con Espacio"
            raiz.mkdir()
            shutil.copy2(SHELL, raiz / "shell.sh")
            (raiz / "profiles.json").write_text(
                json.dumps({"profiles": perfiles}), encoding="utf-8"
            )
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


if __name__ == "__main__":
    unittest.main()
