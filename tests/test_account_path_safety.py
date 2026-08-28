import os
import tempfile
import unittest
from unittest import mock

import orqlib


class AccountPathSafetyTests(unittest.TestCase):
    def test_ids_de_sesion_y_runs_no_pueden_convertirse_en_rutas(self):
        peligroso = "../../otra/ruta"
        seguro = orqlib.id_sesion_seguro(peligroso, "fallback")
        self.assertRegex(seguro, r"^sesion-[0-9a-f]{20}$")
        self.assertEqual(seguro, orqlib.id_sesion_seguro(peligroso, "otro"))
        visible = "20260828-010101-1234"
        self.assertRegex(
            orqlib.id_sesion_seguro(visible, "fallback"), r"^sesion-[0-9a-f]{20}$"
        )
        self.assertNotEqual(orqlib.id_sesion_seguro(visible, "fallback"), visible)
        self.assertRegex(
            orqlib.id_sesion_seguro(None, peligroso), r"^sesion-[0-9a-f]{20}$"
        )
        self.assertFalse(orqlib.run_id_valido("../../salida"))
        self.assertTrue(orqlib.run_id_valido("codex-personal-1234567890123456789"))

    def test_base_python_procede_del_modulo_y_no_de_una_variable(self):
        self.assertEqual(
            orqlib.BASE, os.path.dirname(os.path.realpath(orqlib.__file__))
        )

    def test_ruta_contenida_rechaza_escape_y_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = os.path.join(tmp, "privado")
            fuera = os.path.join(tmp, "fuera")
            os.makedirs(raiz)
            os.makedirs(fuera)
            self.assertIsNone(orqlib.ruta_contenida(raiz, fuera))
            enlace = os.path.join(raiz, "enlace")
            os.symlink(fuera, enlace)
            self.assertIsNone(orqlib.ruta_contenida(raiz, enlace))
            dentro = os.path.join(raiz, "api_key")
            self.assertEqual(orqlib.ruta_contenida(raiz, dentro), dentro)

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
