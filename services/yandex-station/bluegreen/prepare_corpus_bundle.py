#!/usr/bin/env python3
"""Read-only export of an AISIS candidate's exact corpus runtime for RunPod."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parent
FIXTURES = ("run_corpus.mts", "run_dialogs.mts", "intents-ru.jsonl")
SCHEMA = "aisis-corpus-input-v1"

def inspect_image(container: str) -> str:
    result = subprocess.run(["docker", "inspect", container, "--format", "{{.Image}}"],
                            capture_output=True, text=True, check=True, timeout=10)
    image = result.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("invalid_container_image_id")
    return image

def inspect_inputs(container: str) -> dict:
    result = subprocess.run(
        ["docker", "exec", container, "/usr/local/bin/node", "-e",
         (HERE / "inspect_corpus_inputs.cjs").read_text()],
        capture_output=True, text=True, timeout=50,
    )
    if result.returncode:
        raise RuntimeError("candidate_runtime_inventory_failed")
    value = json.loads(result.stdout)
    if not value["file_sha256"] or value["corpus_rows"] < 1:
        raise ValueError("empty_runtime_inventory")
    return value

def stable_digest(manifest: dict) -> str:
    content = {key: value for key, value in manifest.items()
               if key not in {"input_digest", "archive_sha256", "archive_bytes"}}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def export_archive(container: str, names: list[str], archive: Path) -> None:
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        result = subprocess.run(
            ["docker", "exec", "-i", container, "tar", "--format=gnu",
             "--sort=name", "--mtime=@0", "--owner=0", "--group=0", "--numeric-owner",
             "--hard-dereference", "--no-recursion", "--null", "--verbatim-files-from", "-czf", "-", "-C", "/", "-T", "-"],
            input=b"".join(name.lstrip("/").encode() + b"\0" for name in names),
            stdout=output, stderr=subprocess.PIPE, timeout=90,
        )
    if result.returncode:
        raise RuntimeError("candidate_export_failed")

def prepare_corpus_bundle(container: str, corpus_dir: Path, output_dir: Path,
                          logger: logging.Logger | None = None, manifest_only: bool = False) -> dict:
    logger = logger or logging.getLogger(__name__)
    if output_dir.exists():
        raise ValueError("output_directory_already_exists")
    image = inspect_image(container)
    before = inspect_inputs(container)
    for name in FIXTURES:
        local = corpus_dir / name
        digest = hashlib.sha256(local.read_bytes()).hexdigest()
        if before["file_sha256"].get("/src/backend/corpus/" + name) != digest:
            raise ValueError("staged_corpus_differs_from_required_fixture")
    manifest = {"schema": SCHEMA, "image_id": image, **deepcopy(before)}
    manifest["runtime"]["test_environment"] = {
        "AISIS_HA_URL": "http://ha.test", "AISIS_HA_TOKEN": "test-token",
        "AISIS_HA_AMBIGUITY_FILE": "/tmp/aisis-corpus-ambiguity.json",
        "AISIS_ADAPTER_ASR_URL": "http://127.0.0.1:9/asr",
        "AISIS_SETTINGS_URL": "http://127.0.0.1:9/settings",
        "AISIS_ADAPTER_MUSIC_URL": "http://127.0.0.1:9/music",
        "AISIS_ADAPTER_LAYA_URL": "http://127.0.0.1:9/laya",
    }
    names = sorted(set(before["file_sha256"]) | set(before["symlinks"]))
    if any(not name.startswith("/") or ".." in Path(name).parts for name in names):
        raise ValueError("unsafe_export_path")
    logger.info("inventory image=%s files=%s symlinks=%s packages=%s corpus_rows=%s",
                image, len(before["file_sha256"]), len(before["symlinks"]),
                len(before["packages"]), before["corpus_rows"])
    output_dir.mkdir(parents=True, mode=0o700)
    archive = output_dir / "rootfs.tar.gz"
    try:
        if not manifest_only:
            export_archive(container, names, archive)
            after = inspect_inputs(container)
            if inspect_image(container) != image or after != before:
                raise RuntimeError("candidate_changed_during_export")
            with archive.open("rb") as handle:
                manifest["archive_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
            manifest["archive_bytes"] = archive.stat().st_size
        elif inspect_image(container) != image:
            raise RuntimeError("candidate_changed_during_inventory")
        manifest["input_digest"] = stable_digest(manifest)
        path = output_dir / "manifest.json"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(manifest, handle, sort_keys=True, indent=2)
            handle.write("\n")
        logger.info("bundle_ready path=%s digest=%s bytes=%s",
                    output_dir, manifest["input_digest"], manifest.get("archive_bytes", 0))
        return manifest
    except Exception:
        shutil.rmtree(output_dir)
        raise

def main() -> int:
    started = time.monotonic()
    directory = Path(tempfile.mkdtemp(prefix="aisis-corpus-export-"))
    directory.chmod(0o700)
    log_path = directory / "execution.log"
    fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    handle = os.fdopen(fd, "w")
    logger = logging.getLogger("aisis-corpus-export")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = [logging.StreamHandler(), logging.StreamHandler(handle)]
    for handler in logger.handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.info("startup log=%s", log_path)
    status = 1
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--container", required=True)
        parser.add_argument("--corpus-dir", required=True, type=Path)
        parser.add_argument("--output-dir", required=True, type=Path)
        parser.add_argument("--manifest-only", action="store_true")
        args = parser.parse_args()
        manifest = prepare_corpus_bundle(args.container, args.corpus_dir, args.output_dir, logger, args.manifest_only)
        print(json.dumps({"input_digest": manifest["input_digest"], "output_dir": str(args.output_dir)}))
        status = 0
    except Exception as exc:
        logger.error("export_failed type=%s reason=%s", type(exc).__name__, str(exc))
    finally:
        logger.info("completion exit=%s elapsed_seconds=%.3f log=%s",
                    status, time.monotonic() - started, log_path)
        handle.flush()
        handle.close()
    return status

if __name__ == "__main__":
    raise SystemExit(main())
