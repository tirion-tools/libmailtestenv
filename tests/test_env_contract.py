"""The env JSON contract: roles.py's document against schema/env.schema.json, the README example, and the settings
that must be refused (forbidden host ports, foreign container prefixes). Python standard library only:

    python3 -m unittest discover -s tests
"""
import copy
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import roles  # noqa: E402
from schema_check import errors, load_schema  # noqa: E402

ENV = {"MAILTESTENV_STATE": "/srv/state"}


def document(**extra):
    out = io.StringIO()
    with redirect_stdout(out):
        assert roles.main(["env-json"], {**ENV, **extra}) == 0
    return json.loads(out.getvalue())


def refused(**extra):
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        return roles.main(["env-json"], {**ENV, **extra}) == 2


class EnvContract(unittest.TestCase):
    schema = load_schema()

    def assertValid(self, doc):
        self.assertEqual(errors(doc, self.schema), [])

    def test_default_document_is_valid(self):
        doc = document()
        self.assertValid(doc)
        self.assertEqual((doc["schema"], doc["version"], doc["state_dir"]), ("mailtestenv-env", 1, "/srv/state"))
        mail = doc["roles"]["mail"]
        self.assertEqual(mail["container"], "mailtestenv-mail")
        self.assertEqual({k: (e["port"], e["tls"]) for k, e in mail["endpoints"].items()},
                         {"imap": (11143, "starttls"), "imaps": (11993, "implicit"),
                          "submissions": (11465, "implicit"), "submission": (11587, "starttls")})
        self.assertEqual(mail["server_names"][0], "mail.mailtestenv.test")
        self.assertEqual((mail["ca"], mail["secrets"], mail["password"]), ("mail/pki/ca.crt", "mail/secrets.tsv", None))
        self.assertEqual(doc["roles"]["gmail"]["endpoints"]["imap"], {"port": 12143, "tls": "none"})
        self.assertEqual(doc["roles"]["enron"]["accounts_file"], "enron/pristine/MANIFEST.tsv")

    def test_offset_and_prefix(self):
        doc = document(MAILTESTENV_PORT_OFFSET="20000", MAILTESTENV_PREFIX="mailtestenv-gate")
        self.assertValid(doc)
        self.assertEqual(doc["roles"]["apache"]["container"], "mailtestenv-gate-apache")
        self.assertEqual(doc["roles"]["mail"]["endpoints"]["imaps"]["port"], 31993)
        self.assertEqual(doc["port_offset"], 20000)

    def test_every_host_port_is_loopback_and_never_143_or_5232(self):
        for role in roles.ROLES:
            for arg in roles.publish(0, role).split(" -p "):
                host, port, _ = arg.removeprefix("-p ").split(":")
                self.assertEqual(host, "127.0.0.1")
                self.assertNotIn(int(port), (143, 5232))

    def test_forbidden_ports_are_refused(self):
        # 13993 - 8761 = 5232; 11143 - 11000 = 143 (and below 1024 anyway); 12143 - 7000 = 5143 is fine
        self.assertTrue(refused(MAILTESTENV_PORT_OFFSET="-8761"))
        self.assertTrue(refused(MAILTESTENV_PORT_OFFSET="-11000"))
        self.assertTrue(refused(MAILTESTENV_DENY_PORTS="11587"))
        self.assertTrue(refused(MAILTESTENV_PORT_OFFSET="60000"))
        self.assertFalse(refused(MAILTESTENV_PORT_OFFSET="-7000"))

    def test_foreign_prefixes_and_missing_state_are_refused(self):
        for prefix in ("mailreview", "othertestenv", "x", "mailtestenv_UPPER", ""):
            self.assertTrue(refused(MAILTESTENV_PREFIX=prefix), prefix)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(roles.main(["env-json"], {}), 2)

    def test_schema_rejects_broken_documents(self):
        doc = document()
        for mutate in (lambda d: d.update(version=2),
                       lambda d: d["roles"].pop("gmail"),
                       lambda d: d["roles"]["mail"]["endpoints"]["imap"].update(tls="maybe"),
                       lambda d: d["roles"]["mail"]["endpoints"]["imap"].update(port=143),
                       lambda d: d["roles"]["mail"].update(host="0.0.0.0"),
                       lambda d: d["roles"]["mail"].update(ca="/etc/ssl/ca.crt"),
                       lambda d: d["roles"]["mail"].update(extra=1)):
            broken = copy.deepcopy(doc)
            mutate(broken)
            self.assertNotEqual(errors(broken, self.schema), [])

    def test_readme_example_is_the_document(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        m = re.search(r"<!-- env-json-example -->\n```json\n(.*?)\n```", readme, re.S)
        self.assertIsNotNone(m, "README.md has no env JSON example block")
        example = json.loads(m.group(1))
        self.assertValid(example)
        self.assertEqual(example, document(MAILTESTENV_STATE=example["state_dir"]))


if __name__ == "__main__":
    unittest.main()
