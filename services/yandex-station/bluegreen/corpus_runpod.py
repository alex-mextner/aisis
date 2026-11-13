#!/usr/bin/env python3
"""Run the existing AISIS corpus gate on a frozen image through RunPod only."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import posixpath
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve().parent
EXTERNAL_MOUNT = Path("/run/media/ultra/backups")
EXTERNAL_WORK = EXTERNAL_MOUNT / "home-ultra/work"
COMMANDS = (
    ("/node_modules/.bin/tsx", "run_corpus.mts"),
    ("/node_modules/.bin/tsx", "run_dialogs.mts"),
)
CORPUS_CWD = "/src/backend/corpus"
RECEIPT_PREFIX = "AISIS_CORPUS_RECEIPT "
PACKAGING_KEYS = {"input_digest", "archive_sha256", "archive_bytes"}
FLAGS = {"NODE_ENV", "ASR_CORRECT", "AISIS_FAST_EARCON", "SEMANTIC_INTENTS"}
TEST_ENVIRONMENT = {
    "AISIS_HA_URL": "http://ha.test", "AISIS_HA_TOKEN": "test-token",
    "AISIS_HA_AMBIGUITY_FILE": "/tmp/aisis-corpus-ambiguity.json",
    "AISIS_ADAPTER_ASR_URL": "http://127.0.0.1:9/asr",
    "AISIS_SETTINGS_URL": "http://127.0.0.1:9/settings",
    "AISIS_ADAPTER_MUSIC_URL": "http://127.0.0.1:9/music/play",
    "AISIS_ADAPTER_LAYA_URL": "http://127.0.0.1:9/laya",
}
MAX_LOG_BYTES = 2_000_000


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(value: str) -> bool:
    if not isinstance(value, str) or "\x00" in value or "\n" in value:
        return False
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "." in value.split("/"):
        return False
    if value in {"/package.json", "/tsconfig.json", "/usr/local/bin/node"}:
        return True
    if value.startswith("/node_modules/"):
        return True
    if not value.startswith("/src/"):
        return False
    return not re.search(r"(?:^|/)(?:ha\.config|ha\.ambiguity|secrets?|runtime|history)(?:\.|/|$)", value)


def checked_files(source: dict) -> dict:
    files = source.get("file_sha256")
    if not isinstance(files, dict) or not files:
        raise ValueError("missing source hashes")
    for name, digest in files.items():
        if not safe_path(name) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("unsafe source path or invalid hash")
    return files


def check_links(files: dict, links: dict) -> None:
    if not isinstance(links, dict) or set(links) & set(files):
        raise ValueError("ambiguous archive paths")
    for name, target in links.items():
        if not safe_path(name) or not isinstance(target, str) or not target or target.startswith("/"):
            raise ValueError("unsafe link")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        if not safe_path(resolved):
            raise ValueError("link escapes the exported runtime")


def check_runtime(runtime: dict) -> None:
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", runtime.get("node", "")):
        raise ValueError("missing exact Node version")
    flags = runtime.get("flags", {})
    if not isinstance(flags, dict) or set(flags) - FLAGS:
        raise ValueError("private or unsupported runtime flag")
    for value in flags.values():
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{0,64}", value)):
            raise ValueError("unsupported runtime flag value")
    if "test_environment" in runtime and runtime["test_environment"] != TEST_ENVIRONMENT:
        raise ValueError("test environment must use fixed isolated endpoints")


def normalized_manifest(source: dict) -> dict:
    if not isinstance(source, dict) or source.get("schema") != "aisis-corpus-input-v1":
        raise ValueError("invalid input schema")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", source.get("image_id", "")):
        raise ValueError("invalid image identity")
    files = checked_files(source)
    check_links(files, source.get("symlinks", {}))
    check_runtime(source.get("runtime", {}))
    if type(source.get("corpus_rows")) is not int or source["corpus_rows"] < 1:
        raise ValueError("empty corpus")
    return {key: value for key, value in source.items() if key not in PACKAGING_KEYS}

def input_digest(source: dict) -> str:
    content = normalized_manifest(source)
    value = digest_bytes(json.dumps(content, sort_keys=True, separators=(",", ":")).encode())
    if source.get("input_digest", value) != value:
        raise ValueError("manifest digest mismatch")
    return value


def execution_env(source: dict) -> dict[str, str]:
    normalized_manifest(source)
    # Never inherit host/container credentials, configuration paths or history.
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C.UTF-8"}
    environment.update(source["runtime"].get("test_environment", {}))
    environment.update({key: value for key, value in source["runtime"]["flags"].items()
                        if value is not None})
    return environment


def summary(output: str, kind: str, expected: int | None) -> dict:
    marker = "ИТОГ" if kind == "corpus" else "ДИАЛОГИ"
    lines = [line.strip() for line in output.splitlines() if line.strip().startswith(marker)]
    if len(lines) != 1:
        raise ValueError(f"{kind}: exactly one complete summary is required")
    match = re.fullmatch(re.escape(marker) + r":\s*([0-9]+)/([0-9]+)(?:\s*\([0-9.]+%\))?", lines[0])
    if match is None:
        raise ValueError(f"{kind}: malformed summary")
    passed, total = map(int, match.groups())
    if total < 1 or passed != total or (expected is not None and total != expected):
        raise ValueError(f"{kind}: incomplete or failing required suite")
    return {"passed": passed, "total": total}



def verify_run(run: dict, command: tuple, kind: str, expected: int | None) -> dict:
    if run.get("argv") != list(command) or run.get("cwd") != CORPUS_CWD:
        raise ValueError("required command or source working directory was changed")
    if type(run.get("exit")) is not int or run["exit"] != 0:
        raise ValueError(f"{kind}: process exit was not successful")
    output = run.get("output")
    if not isinstance(output, str) or len(output.encode()) > MAX_LOG_BYTES:
        raise ValueError("missing or excessive test log")
    if run.get("output_sha256") != digest_bytes(output.encode()):
        raise ValueError("test log hash does not match")
    return summary(output, kind, expected)


def verify_receipt(*, before: dict, after: dict, receipt: dict, runner_exit: int) -> dict:
    expected = input_digest(before)
    if input_digest(after) != expected:
        raise ValueError("candidate image, source or runtime changed during the gate")
    if type(runner_exit) is not int or runner_exit != 0:
        raise ValueError("managed runner did not succeed")
    if not isinstance(receipt, dict) or receipt.get("schema") != "aisis-corpus-receipt-v1":
        raise ValueError("missing remote execution receipt")
    if receipt.get("input_digest") != expected:
        raise ValueError("receipt belongs to different input")
    runs = receipt.get("runs")
    if not isinstance(runs, list) or len(runs) != len(COMMANDS):
        raise ValueError("both original commands are required exactly once")
    result = {}
    for index, (kind, command) in enumerate(zip(("corpus", "dialogs"), COMMANDS)):
        rows = before["corpus_rows"] if kind == "corpus" else None
        result[kind] = verify_run(runs[index], command, kind, rows)
    return result


def verify_files(root: Path, source: dict) -> None:
    normalized_manifest(source)
    declared = set(source["file_sha256"]) | set(source.get("symlinks", {}))
    actual = {"/" + path.relative_to(root).as_posix() for path in root.rglob("*")
              if path.is_file() or path.is_symlink()}
    if actual != declared:
        raise ValueError("runtime has missing or undeclared source entries")
    for name, digest in source["file_sha256"].items():
        path = root / name.lstrip("/")
        if path.is_symlink() or not path.is_file() or digest_file(path) != digest:
            raise ValueError("exported file bytes differ from frozen input")
    for name, target in source.get("symlinks", {}).items():
        path = root / name.lstrip("/")
        if not path.is_symlink() or os.readlink(path) != target:
            raise ValueError("exported symlink differs from frozen input")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError("exported symlink escapes runtime root")

def checked_members(tar: tarfile.TarFile, source: dict) -> list:
    declared = set(source["file_sha256"]) | set(source.get("symlinks", {}))
    members = tar.getmembers()
    names = ["/" + member.name for member in members]
    if len(names) != len(set(names)) or set(names) != declared:
        raise ValueError("archive entries do not match the complete declared input")
    for member, name in zip(members, names):
        if not safe_path(name) or member.name.startswith("/"):
            raise ValueError("unsafe archive path")
        if name in source["file_sha256"]:
            if not member.isfile():
                raise ValueError("regular source replaced by an archive link")
        elif not member.issym() or member.linkname != source["symlinks"][name]:
            raise ValueError("unexpected archive link")
    return members


def materialize_members(tar: tarfile.TarFile, members: list, source: dict, root: Path) -> None:
    for member in members:
        if not member.isfile():
            continue
        destination = root / member.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tar.extractfile(member) as original, destination.open("xb") as target:
            shutil.copyfileobj(original, target)
        destination.chmod(source.get("file_mode", {}).get("/" + member.name, member.mode) & 0o777)
    for member in members:
        if member.issym():
            destination = root / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.symlink_to(member.linkname)


def extract_payload(bundle: Path, source: dict) -> Path:
    input_digest(source)
    archive = bundle / "rootfs.tar.gz"
    if source.get("archive_sha256") != digest_file(archive):
        raise ValueError("archive digest mismatch")
    root = bundle / "rootfs"
    if root.exists():
        verify_files(root, source)
        return root
    with tarfile.open(archive, "r:gz") as tar:
        members = checked_members(tar, source)
        root.mkdir(mode=0o755)
        try:
            materialize_members(tar, members, source, root)
            verify_files(root, source)
        except Exception:
            shutil.rmtree(root)
            raise
    return root

def claim_alias(alias: Path, target: Path) -> None:
    if alias.exists() or alias.is_symlink():
        if not alias.is_symlink() or alias.resolve() != target.resolve():
            raise ValueError("pod application path is already owned")
        return
    alias.symlink_to(target)


def bootstrap(bundle: Path, expected: str, logger: logging.Logger) -> None:
    source = json.loads((bundle / "manifest.json").read_text())
    if input_digest(source) != expected:
        raise ValueError("bootstrap input changed")
    root = extract_payload(bundle, source)
    for name in ("/src", "/node_modules", "/package.json", "/tsconfig.json"):
        claim_alias(Path(name), root / name.lstrip("/"))
    uid = pwd.getpwnam("runner").pw_uid
    gid = pwd.getpwnam("runner").pw_gid
    for path in (root, *root.rglob("*")):
        os.chown(path, uid, gid, follow_symlinks=False)
    logger.info("bootstrap verified files=%s symlinks=%s node=%s",
                len(source["file_sha256"]), len(source.get("symlinks", {})), source["runtime"]["node"])

def remote_run(bundle: Path, logger: logging.Logger) -> int:
    source = json.loads((bundle / "manifest.json").read_text())
    root = bundle / "rootfs"
    verify_files(root, source)
    environment = execution_env(source)
    environment["PATH"] = str(root / "usr/local/bin") + ":/usr/bin:/bin"
    node = subprocess.check_output([str(root / "usr/local/bin/node"), "--version"],
                                   text=True, env=environment, timeout=10).strip()
    if node != source["runtime"]["node"]:
        raise ValueError("Node runtime version does not match the image")
    receipt = {"schema": "aisis-corpus-receipt-v1", "input_digest": input_digest(source), "runs": []}
    for command in COMMANDS:
        logger.info("required_test_start command=%s cwd=%s", list(command), CORPUS_CWD)
        started = time.monotonic()
        result = subprocess.run(command, cwd=CORPUS_CWD, env=environment,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=420)
        if len(result.stdout.encode()) > MAX_LOG_BYTES:
            raise ValueError("test log exceeded the bounded capture")
        receipt["runs"].append({"argv": list(command), "cwd": CORPUS_CWD, "exit": result.returncode,
                                "output": result.stdout, "output_sha256": digest_bytes(result.stdout.encode())})
        logger.info("required_test_end command=%s exit=%s seconds=%.3f output_sha256=%s",
                    command[1], result.returncode, time.monotonic() - started,
                    receipt["runs"][-1]["output_sha256"])
    verify_files(root, source)
    # Full bounded logs travel inside the receipt so native runner dedup retains them.
    print(RECEIPT_PREFIX + json.dumps(receipt, separators=(",", ":")), flush=True)
    counts = verify_receipt(before=source, after=source, receipt=receipt, runner_exit=0)
    logger.info("all_required_tests_passed counts=%s", counts)
    return 0


def logged_command(command: list[str], logger: logging.Logger, *, cwd: Path | None = None) -> tuple[int, str]:
    logger.info("command_start executable=%s arguments=%s", command[0], command[1:])
    process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = []
    for line in process.stdout:
        output.append(line)
        logger.info("runner %s", line.rstrip())
    return process.wait(), "".join(output)


def export_snapshot(container: str, corpus_dir: Path, destination: Path,
                    logger: logging.Logger, *, manifest_only: bool = False) -> dict:
    command = [sys.executable, str(HERE / "prepare_corpus_bundle.py"), "--container", container,
               "--corpus-dir", str(corpus_dir), "--output-dir", str(destination)]
    if manifest_only:
        command.append("--manifest-only")
    code, _ = logged_command(command, logger)
    if code:
        raise ValueError("fresh image export failed")
    source = json.loads((destination / "manifest.json").read_text())
    input_digest(source)
    return source


def external_state_root() -> Path:
    if not os.path.ismount(EXTERNAL_MOUNT):
        raise ValueError("required external disk is not mounted; no fallback directory is allowed")
    if not EXTERNAL_WORK.is_dir() or not EXTERNAL_WORK.resolve().is_relative_to(EXTERNAL_MOUNT.resolve()):
        raise ValueError("external work directory is missing or redirects outside the mounted disk")
    return EXTERNAL_WORK / "aisis-corpus-inputs"


def bundle_key(source: dict, helper_digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", helper_digest):
        raise ValueError("invalid gate helper digest")
    return digest_bytes((input_digest(source) + helper_digest).encode())


def managed_command(bundle: Path, source_id: str) -> list[str]:
    if not re.fullmatch(r"[0-9a-f]{64}", source_id):
        raise ValueError("invalid managed input identity")
    wrapper = Path.home() / ".local/bin/runpod-run"
    # No custom pod name and no retained pod. Native wrapper identity is this
    # input's independent Git common directory; changed input creates a new pod.
    return [str(wrapper), "--sync-dir", str(bundle),
            "--remote-reason", "Required AISIS adversarial corpus gate must run on RunPod",
            "--timeout", "900", "--bootstrap",
            "python3 corpus_runpod.py --bootstrap " + source_id,
            "--", "python3", "corpus_runpod.py", "--remote"]


def prepare_input_bundle(state: Path, snapshot: Path, source: dict) -> Path:
    bundle = state / bundle_key(source, digest_file(Path(__file__)))
    bundle.mkdir(exist_ok=True, mode=0o700)
    for name in ("manifest.json", "rootfs.tar.gz"):
        incoming, target = snapshot / name, bundle / name
        if target.exists() and digest_file(target) != digest_file(incoming):
            raise ValueError("immutable bundle path already holds different bytes")
        if not target.exists():
            shutil.copy2(incoming, target)
    shutil.copy2(Path(__file__), bundle / "corpus_runpod.py")
    # runpod-run requires its primary input to be a Git checkout. This is an
    # artifact-only repository: no application history or credentials are copied.
    subprocess.run(["git", "init", "--quiet", str(bundle)], check=True, timeout=10)
    subprocess.run(["git", "-C", str(bundle), "add", "--",
                    "manifest.json", "rootfs.tar.gz", "corpus_runpod.py"], check=True, timeout=20)
    return bundle


def save_proof(state: Path, before: dict, receipt: dict, counts: dict, runner_exit: int) -> Path:
    source_id = input_digest(before)
    evidence = state / "evidence"
    evidence.mkdir(exist_ok=True, mode=0o700)
    path = evidence / (source_id + "-" + str(time.time_ns()) + ".json")
    proof = {"input_digest": source_id, "image_id": before["image_id"], "counts": counts,
             "receipt": receipt, "runner_exit": runner_exit}
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(proof, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return path


def local_gate(container: str, corpus_dir: Path, logger: logging.Logger) -> int:
    state = external_state_root()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix="snapshot-", dir=state))
    try:
        before = export_snapshot(container, corpus_dir, temporary / "before", logger)
        source_id = input_digest(before)
        bundle = prepare_input_bundle(state, temporary / "before", before)
        command = managed_command(bundle, source_id)
        if not Path(command[0]).is_file():
            raise ValueError("established runpod-run is unavailable; no local test fallback")
        runner_exit, output = logged_command(command, logger, cwd=bundle)
        after = export_snapshot(container, corpus_dir, temporary / "after", logger, manifest_only=True)
        receipts = [line[len(RECEIPT_PREFIX):] for line in output.splitlines()
                    if line.startswith(RECEIPT_PREFIX)]
        if len(receipts) != 1:
            raise ValueError("managed runner did not return exactly one execution receipt")
        receipt = json.loads(receipts[0])
        counts = verify_receipt(before=before, after=after, receipt=receipt, runner_exit=runner_exit)
        path = save_proof(state, before, receipt, counts, runner_exit)
        logger.info("gate_passed image=%s digest=%s counts=%s proof=%s",
                    before["image_id"], source_id, counts, path)
        return 0
    finally:
        shutil.rmtree(temporary)

def main() -> int:
    started = time.monotonic()
    directory = Path(tempfile.mkdtemp(prefix="aisis-corpus-gate-"))
    directory.chmod(0o700)
    log_path = directory / "execution.log"
    fd = os.open(log_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    handle = os.fdopen(fd, "w")
    logger = logging.getLogger("aisis-corpus-gate")
    logger.setLevel(logging.INFO)
    logger.handlers[:] = [logging.StreamHandler(), logging.StreamHandler(handle)]
    for handler in logger.handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.info("startup log=%s", log_path)
    status = 1
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--container")
        group.add_argument("--remote", action="store_true")
        group.add_argument("--bootstrap")
        parser.add_argument("--corpus-dir", type=Path)
        args = parser.parse_args()
        if args.bootstrap:
            bootstrap(Path.cwd(), args.bootstrap, logger)
            status = 0
        elif args.remote:
            status = remote_run(Path.cwd(), logger)
        else:
            if args.corpus_dir is None:
                raise ValueError("corpus directory is required")
            status = local_gate(args.container, args.corpus_dir, logger)
    except Exception as exc:
        logger.error("gate_failed type=%s reason=%s", type(exc).__name__, str(exc))
    finally:
        logger.info("completion exit=%s elapsed_seconds=%.3f log=%s",
                    status, time.monotonic() - started, log_path)
        handle.flush()
        handle.close()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
