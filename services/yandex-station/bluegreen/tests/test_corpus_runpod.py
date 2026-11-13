"""Gate contracts; the release gate separately runs the original whole corpus."""
from __future__ import annotations
import copy
import hashlib
import importlib.util
import json
import io
import tarfile
import tempfile
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "corpus_runpod.py"
SPEC = importlib.util.spec_from_file_location("aisis_corpus_runpod", MODULE_PATH)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(gate)

def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()

def manifest():
    return {
        "schema": "aisis-corpus-input-v1",
        "image_id": "sha256:" + "1" * 64,
        "file_sha256": {
            "/src/backend/ha-intents.ts": "2" * 64,
            "/src/backend/corpus/run_corpus.mts": "3" * 64,
            "/src/backend/corpus/run_dialogs.mts": "4" * 64,
            "/src/backend/corpus/intents-ru.jsonl": "5" * 64,
            "/node_modules/tsx/package.json": "6" * 64,
        },
        "runtime": {"node": "v22.18.0", "flags": {"NODE_ENV": "production"}},
        "corpus_rows": 2,
    }

def receipt(source):
    commands = (
        ("/node_modules/.bin/tsx", "run_corpus.mts"),
        ("/node_modules/.bin/tsx", "run_dialogs.mts"),
    )
    logs = ("ИТОГ: 2/2 (100.0%)\n", "ДИАЛОГИ: 17/17\n")
    return {
        "schema": "aisis-corpus-receipt-v1",
        "input_digest": gate.input_digest(source),
        "runs": [
            {"argv": list(argv), "cwd": "/src/backend/corpus", "exit": 0,
             "output": output, "output_sha256": sha(output)}
            for argv, output in zip(commands, logs)
        ],
    }

class CorpusGateContracts(unittest.TestCase):
    def setUp(self):
        self.source = manifest()
        self.proof = receipt(self.source)

    def verify(self, **changes):
        args = {"before": self.source, "after": copy.deepcopy(self.source),
                "receipt": self.proof, "runner_exit": 0}
        args.update(changes)
        return gate.verify_receipt(**args)

    def test_complete_original_commands_and_full_counts_are_required(self):
        result = self.verify()
        self.assertEqual(result["corpus"], {"passed": 2, "total": 2})
        self.assertEqual(result["dialogs"], {"passed": 17, "total": 17})

    def test_nonzero_runner_or_test_exit_cannot_be_hidden_by_success_summary(self):
        with self.assertRaises(ValueError):
            self.verify(runner_exit=1)
        for index in (0, 1):
            with self.subTest(index=index):
                proof = copy.deepcopy(self.proof)
                proof["runs"][index]["exit"] = 1
                with self.assertRaises(ValueError):
                    self.verify(receipt=proof)

    def test_corpus_incomplete_failed_missing_or_duplicate_summary_is_rejected(self):
        for output in ("ИТОГ: 1/2\n", "ИТОГ: 1/1\n", "ИТОГ: 0/0\n",
                       "all okay\n", "ИТОГ: 2/2\nИТОГ: 2/2\n"):
            with self.subTest(output=output):
                proof = copy.deepcopy(self.proof)
                proof["runs"][0].update(output=output, output_sha256=sha(output))
                with self.assertRaises(ValueError):
                    self.verify(receipt=proof)

    def test_dialogs_failed_empty_missing_or_duplicate_summary_is_rejected(self):
        for output in ("ДИАЛОГИ: 16/17\n", "ДИАЛОГИ: 0/0\n",
                       "all okay\n", "ДИАЛОГИ: 17/17\nДИАЛОГИ: 17/17\n"):
            with self.subTest(output=output):
                proof = copy.deepcopy(self.proof)
                proof["runs"][1].update(output=output, output_sha256=sha(output))
                with self.assertRaises(ValueError):
                    self.verify(receipt=proof)

    def test_recreated_container_changed_source_or_flags_invalidates_the_run(self):
        for change in ("image", "source", "runtime"):
            with self.subTest(change=change):
                after = copy.deepcopy(self.source)
                if change == "image":
                    after["image_id"] = "sha256:" + "9" * 64
                elif change == "source":
                    after["file_sha256"]["/src/backend/ha-intents.ts"] = "9" * 64
                else:
                    after["runtime"]["flags"]["NODE_ENV"] = "test"
                with self.assertRaises(ValueError):
                    self.verify(after=after)
        proof = copy.deepcopy(self.proof)
        proof["input_digest"] = "a" * 64
        with self.assertRaises(ValueError):
            self.verify(receipt=proof)

    def test_tampered_logs_wrong_commands_partial_or_extra_runs_are_rejected(self):
        variants = []
        proof = copy.deepcopy(self.proof)
        proof["runs"][0]["output"] = "ИТОГ: 200/200\n"
        variants.append(proof)
        proof = copy.deepcopy(self.proof)
        proof["runs"][0]["argv"].append("--skip")
        variants.append(proof)
        proof = copy.deepcopy(self.proof)
        proof["runs"][0]["cwd"] = "/another/source"
        variants.append(proof)
        proof = copy.deepcopy(self.proof)
        proof["runs"].reverse()
        variants.append(proof)
        proof = copy.deepcopy(self.proof)
        proof["runs"].pop()
        variants.append(proof)
        proof = copy.deepcopy(self.proof)
        proof["runs"].append(proof["runs"][0])
        variants.append(proof)
        for number, proof in enumerate(variants):
            with self.subTest(number=number):
                with self.assertRaises(ValueError):
                    self.verify(receipt=proof)

    def test_private_environment_and_path_escape_are_not_valid_bundle_inputs(self):
        for key in ("AISIS_HA_TOKEN", "OPENAI_API_KEY", "DATABASE_URL"):
            with self.subTest(key=key):
                source = copy.deepcopy(self.source)
                source["runtime"]["flags"][key] = "private-sentinel"
                with self.assertRaises(ValueError):
                    gate.input_digest(source)
        for key in ("/src/../../config/secrets.json", "/history/user.json", "/src/link/../secret"):
            with self.subTest(path=key):
                source = copy.deepcopy(self.source)
                source["file_sha256"][key] = "a" * 64
                with self.assertRaises(ValueError):
                    gate.input_digest(source)

    def test_execution_environment_never_inherits_live_credentials(self):
        source = copy.deepcopy(self.source)
        old = {key: os.environ.get(key) for key in ("AISIS_HA_TOKEN", "DATABASE_URL")}
        os.environ["AISIS_HA_TOKEN"] = "never-forward-this-real-token"
        os.environ["DATABASE_URL"] = "never-forward-this-live-database"
        try:
            environment = gate.execution_env(source)
            completed = subprocess.run(
                [sys.executable, "-c",
                 "import json,os; print(json.dumps({k:os.environ.get(k) for k in ('AISIS_HA_TOKEN','DATABASE_URL','NODE_ENV')}))"],
                check=True, capture_output=True, text=True, env=environment,
            )
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        actual = json.loads(completed.stdout)
        self.assertIsNone(actual["AISIS_HA_TOKEN"])
        self.assertIsNone(actual["DATABASE_URL"])
        self.assertEqual(actual["NODE_ENV"], "production")

class CorpusPayloadContracts(unittest.TestCase):
    def test_archive_cannot_escape_or_write_a_link_in_place_of_a_regular_file(self):
        for entry, link in (("../../outside-proof", None),
                            ("src/backend/ha-intents.ts", "../../../../outside-proof")):
            with self.subTest(entry=entry, link=link), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory) / "bundle"
                bundle.mkdir()
                archive = bundle / "rootfs.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    info = tarfile.TarInfo(entry)
                    if link:
                        info.type = tarfile.SYMTYPE
                        info.linkname = link
                        tar.addfile(info)
                    else:
                        info.size = 4
                        tar.addfile(info, io.BytesIO(b"bad!"))
                source = manifest()
                source["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
                with self.assertRaises(ValueError):
                    gate.extract_payload(bundle, source)
                self.assertFalse((Path(directory) / "outside-proof").exists())

    def test_archive_bytes_are_verified_before_unpacking(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "rootfs.tar.gz").write_bytes(b"corrupted payload")
            source = manifest()
            source["archive_sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                gate.extract_payload(bundle, source)
            self.assertFalse((bundle / "rootfs").exists())

class CorpusWorkspaceContracts(unittest.TestCase):
    def test_external_mount_is_required_before_state_directory_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            mount = Path(directory) / "mounted"
            work = mount / "home-ultra/work"
            work.mkdir(parents=True)
            with patch.object(gate, "EXTERNAL_MOUNT", mount), patch.object(gate, "EXTERNAL_WORK", work):
                with patch.object(gate.os.path, "ismount", return_value=False):
                    with self.assertRaises(ValueError):
                        gate.external_state_root()
                    self.assertFalse((work / "aisis-corpus-inputs").exists())
                with patch.object(gate.os.path, "ismount", return_value=True):
                    self.assertEqual(gate.external_state_root(), work / "aisis-corpus-inputs")

    def test_external_work_symlink_cannot_redirect_to_an_unmounted_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            mount, outside = Path(directory) / "mounted", Path(directory) / "ssd"
            mount.mkdir()
            outside.mkdir()
            work = mount / "work"
            work.symlink_to(outside)
            with patch.object(gate, "EXTERNAL_MOUNT", mount), patch.object(gate, "EXTERNAL_WORK", work):
                with patch.object(gate.os.path, "ismount", return_value=True):
                    with self.assertRaises(ValueError):
                        gate.external_state_root()

    def test_reused_runtime_rejects_undeclared_files_before_executing_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = manifest()
            for name in source["file_sha256"]:
                content = ("fixture:" + name).encode()
                target = root / name.lstrip("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                source["file_sha256"][name] = hashlib.sha256(content).hexdigest()
            gate.verify_files(root, source)
            (root / "src/backend/undeclared.ts").write_text("export default 'changed runtime'")
            with self.assertRaises(ValueError):
                gate.verify_files(root, source)

    def test_new_image_or_flags_has_a_distinct_native_runner_identity_without_kept_pod(self):
        source = manifest()
        changed = copy.deepcopy(source)
        changed["runtime"]["flags"]["NODE_ENV"] = "test"
        first = gate.bundle_key(source, "a" * 64)
        second = gate.bundle_key(changed, "a" * 64)
        self.assertNotEqual(first, second)
        for key, item in ((first, source), (second, changed)):
            command = gate.managed_command(Path("/external/work") / key, gate.input_digest(item))
            self.assertEqual(command[command.index("--sync-dir") + 1], "/external/work/" + key)
            for unsupported in ("--name", "--ttl", "--keep", "--root"):
                self.assertNotIn(unsupported, command)

    def test_alias_claim_never_replaces_a_foreign_path_or_another_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "input-a", root / "input-b"
            first.mkdir()
            second.mkdir()
            alias = root / "src"
            gate.claim_alias(alias, first)
            gate.claim_alias(alias, first)
            with self.assertRaises(ValueError):
                gate.claim_alias(alias, second)
            self.assertEqual(alias.resolve(), first.resolve())
            foreign = root / "owned-by-another"
            foreign.mkdir()
            with self.assertRaises(ValueError):
                gate.claim_alias(foreign, first)
            self.assertTrue(foreign.is_dir())
            self.assertFalse(foreign.is_symlink())

if __name__ == "__main__":
    unittest.main()
