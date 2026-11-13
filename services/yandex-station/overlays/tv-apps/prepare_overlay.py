#!/usr/bin/env python3
"""Build a minimal source overlay from the exact reviewed production matcher."""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

BASE_SHA256 = "d27745ec7670384fe960b409f3ccdfed4f1043773a31a8c9b38671041c85cebc"
PATCHED_SHA256 = "245c920baafb0709493e30547c147ebc50d475513595c10c4663708478a3cca9"
BASE_IMAGE_ID = "sha256:b05533500fe7e99c90cb47137ac3a5af99aaa71034fc3ccb01dbaa69e72661f9"
HERE = Path(__file__).resolve().parent

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> int:
    started = time.monotonic()
    log_dir = Path(tempfile.mkdtemp(prefix="aisis-tv-app-overlay-"))
    log_dir.chmod(0o700)
    log_path = log_dir / "execution.log"
    fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    stream = os.fdopen(fd, "w")
    logger = logging.getLogger("aisis-tv-app-overlay")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = [logging.StreamHandler(), logging.StreamHandler(stream)]
    for handler in logger.handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.info("startup log=%s", log_path)
    result = 1
    created = None
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--baseline", required=True, type=Path)
        parser.add_argument("--output-dir", required=True, type=Path)
        args = parser.parse_args()
        if digest(args.baseline) != BASE_SHA256:
            raise RuntimeError("baseline_source_mismatch")
        if args.output_dir.exists():
            raise RuntimeError("output_directory_already_exists")
        args.output_dir.mkdir(parents=True, mode=0o700)
        created = args.output_dir
        backend = args.output_dir / "backend"
        backend.mkdir()
        shutil.copyfile(args.baseline, backend / "ha-intents.ts")
        proc = subprocess.run(
            ["patch", "--batch", "--fuzz=0", "-p0", "-i", str(HERE / "ha-intents.patch")],
            cwd=args.output_dir, capture_output=True, text=True, timeout=10,
        )
        logger.info("apply_reviewed_patch exit=%s", proc.returncode)
        if proc.returncode != 0:
            raise RuntimeError("reviewed_patch_did_not_apply")
        if digest(backend / "ha-intents.ts") != PATCHED_SHA256:
            raise RuntimeError("patched_source_mismatch")
        shutil.copyfile(HERE / "src/backend/tv-app-intents.ts", backend / "tv-app-intents.ts")
        manifest = {
            "base_image_id": BASE_IMAGE_ID,
            "baseline_sha256": BASE_SHA256,
            "files": {
                path.relative_to(args.output_dir).as_posix(): digest(path)
                for path in sorted(args.output_dir.rglob("*")) if path.is_file()
            },
        }
        manifest_path = args.output_dir.parent / (args.output_dir.name + ".manifest.json")
        if manifest_path.exists():
            raise RuntimeError("manifest_already_exists")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        logger.info("overlay_ready directory=%s manifest=%s files=%s",
                    args.output_dir, manifest_path, len(manifest["files"]))
        result = 0
    except Exception as exc:
        logger.error("preparation_failed type=%s reason=%s", type(exc).__name__, str(exc))
        if created is not None:
            shutil.rmtree(created)
    finally:
        logger.info("completion exit=%s elapsed_seconds=%.3f log=%s",
                    result, time.monotonic() - started, log_path)
        stream.flush()
        stream.close()
    return result

if __name__ == "__main__":
    raise SystemExit(main())
