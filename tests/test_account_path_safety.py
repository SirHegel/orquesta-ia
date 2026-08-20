import os
import tempfile
import unittest
from unittest import mock

import orqlib


class AccountPathSafetyTests(unittest.TestCase):
    def test_entorno_no_hereda_endpoint_ni_credencial_de_otro_proveedor(self):
        contaminado = {
            "ANTHROPIC_BASE_URL": "https://minimax.invalid",
            "ANTHROPIC_AUTH_TOKEN": "secreto-cruzado",
            "ANTHROPIC_MODEL": "modelo-minimax",
            "GEMINI_API_KEY": "secreto-gemini",
            "OPENAI_API_KEY": "secreto-openai",
        }
        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            orqlib, "BASE", base
        ), mock.patch.dict(os.environ, contaminado, clear=False):
            for provider in ("claude", "gpt"):
                with self.subTest(provider=provider):
                    env = orqlib.entorno("cuenta", {"provider": provider})
                    for variable in contaminado:
                        self.assertNotIn(variable, env)

    def test_home_relativo_se_resuelve_desde_el_clon_no_desde_el_cwd(self):
        with tempfile.TemporaryDirectory() as base, mock.patch.object(
            orqlib, "BASE", base
        ):
            self.assertEqual(
                orqlib.home_de("gpt-personal", {"home": "accounts/gpt-personal"}),
                os.path.join(base, "accounts", "gpt-personal"),
            )

    def test_purge_solo_acepta_el_directorio_directo_de_la_cuenta(self):
        with tempfile.TemporaryDirectory() as base:
            accounts = os.path.join(base, "accounts")
            os.makedirs(accounts)
            with mock.patch.object(orqlib, "BASE", base), mock.patch.object(
                orqlib, "ACCOUNTS", accounts
            ):
                self.assertTrue(
                    orqlib.home_purgable("cuenta-a", {"home": "accounts/cuenta-a"})
                )
                self.assertFalse(
                    orqlib.home_purgable("cuenta-a", {"home": "accounts/.."})
                )
                self.assertFalse(
                    orqlib.home_purgable("cuenta-a", {"home": "accounts-otro/cuenta-a"})
                )
                fuera = os.path.join(base, "fuera")
                os.makedirs(fuera)
                os.symlink(fuera, os.path.join(accounts, "cuenta-a"))
                self.assertFalse(
                    orqlib.home_purgable("cuenta-a", {"home": "accounts/cuenta-a"})
                )


if __name__ == "__main__":
    unittest.main()
