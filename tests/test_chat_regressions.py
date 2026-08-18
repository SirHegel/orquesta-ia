import contextlib
import io
import re
import unittest
from unittest import mock

import orqchat


ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class PromptEntradaTests(unittest.TestCase):
    def test_prompt_ansi_esta_marcado_para_readline_y_mide_tres_columnas(self):
        with mock.patch.object(orqchat, "readline", object()):
            prompt = orqchat.prompt_entrada()

        self.assertEqual(
            prompt,
            f" \x01{orqchat.R}\x02▍\x01{orqchat.N}\x02 ",
        )
        visible = ANSI.sub("", prompt.replace("\x01", "").replace("\x02", ""))
        self.assertEqual(visible, " ▍ ")
        self.assertEqual(len(visible), 3)


class FailoverKittiTests(unittest.TestCase):
    def test_responder_prueba_el_siguiente_candidato_tras_error_rc(self):
        antigravity = {"provider": "antigravity"}
        polidinamica = {"provider": "claude"}
        candidatos = [
            {"pid": "gemini-antigravity", "p": antigravity},
            {"pid": "claude-polidinamica", "p": polidinamica},
        ]
        fallo = {
            "perfil": "gemini-antigravity",
            "texto": "[ERROR rc=1] RESOURCE_EXHAUSTED",
            "tokens": 0,
            "seg": 0.2,
            "rc": 1,
        }
        exito = {
            "perfil": "claude-polidinamica",
            "texto": "respuesta util",
            "tokens": 17,
            "seg": 0.4,
            "rc": 0,
        }

        salida = io.StringIO()
        with (
            mock.patch.object(orqchat.L, "ranking", return_value=candidatos),
            mock.patch.object(orqchat.L, "correr", side_effect=[fallo, exito]) as correr,
            mock.patch.object(orqchat, "marco_carpeta", return_value="MARCO\n"),
            mock.patch.object(orqchat, "con_contexto", return_value="PREGUNTA"),
            mock.patch.object(
                orqchat,
                "Girador",
                side_effect=lambda _texto: contextlib.nullcontext(),
            ),
            contextlib.redirect_stdout(salida),
        ):
            resultado = orqchat.responder(
                "hola", [], tarea="reasoning", forzado=None
            )

        self.assertIs(resultado, exito)
        self.assertEqual(correr.call_count, 2)
        self.assertEqual(
            [llamada.args[0] for llamada in correr.call_args_list],
            ["gemini-antigravity", "claude-polidinamica"],
        )
        self.assertEqual(correr.call_args_list[0].args[2], "MARCO\nPREGUNTA")
        self.assertIn("probando claude-polidinamica", salida.getvalue())


if __name__ == "__main__":
    unittest.main()
