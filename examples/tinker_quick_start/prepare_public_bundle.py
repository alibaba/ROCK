"""Prepare fresh public offline Docker COPY inputs; never run Docker or a model.

Shared source verification and Docker context helpers for prepare_sandbox.py.
The supplied rl-rock wheel is built separately from the user's ROCK checkout.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import email
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile

COMMIT = "3ea751c087f32b16e039a2233dd6eefecef325d5"
SOURCE_SHA = "a02724577cde3c7033343efff74184ad33ca783b9f3b81bea90c13f4c47219a4"
PBS_NAME = "cpython-3.12.15+20261003-x86_64-unknown-linux-gnu-install_only.tar.gz"
ASSETS = {
    "swe-agent-source.tar.gz": (f"https://codeload.github.com/SWE-agent/SWE-agent/tar.gz/{COMMIT}", SOURCE_SHA),
    PBS_NAME: ("https://github.com/astral-sh/python-build-standalone/releases/download/20261003/" + PBS_NAME.replace("+", "%2B"), "f937814031eab4698ca6d07ec606ede1825768f3f3e99af76d9db3900bee03c5"),
    "docker-29.8.2.tgz": ("https://download.docker.com/linux/static/stable/x86_64/docker-29.8.2.tgz", "995d1ef289677f74fd58d8d2c35727b6a4ee389c69db8638a3e42d0487aa5b0f"),
    "docker-compose-linux-x86_64": ("https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-x86_64", "7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4"),
}
ENCODINGS = {
    "cl100k_base": "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    "o200k_base": "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
}
SDIST_BUILD = {"oss2": "2.19.1", "crcmod": "1.7", "cellpylib": "2.4.0", "zss": "1.2.0", "pycosat": "0.6.6"}


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    temporary = Path(str(path) + ".partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def fetch(url, path, expected, cache=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing asset symlink")
    if path.exists():
        if sha(path) != expected:
            raise ValueError(f"Existing asset checksum mismatch: {path.name}")
        return "existing_verified_file"
    partial = Path(str(path) + ".partial")
    if partial.is_symlink():
        raise ValueError("Refusing partial symlink")
    retrieval = "public_download"
    cached = Path(cache) / expected if cache else None
    if cached and cached.exists():
        if cached.is_symlink() or sha(cached) != expected:
            raise ValueError("Public download cache checksum mismatch")
        shutil.copyfile(cached, partial)
        retrieval = "sha256_download_cache"
    elif url.startswith(("https://github.com/", "https://codeload.github.com/", "https://download.docker.com/")):
        subprocess.run(["curl", "-4", "--silent", "--show-error", "--fail", "--location",
                        "--connect-timeout", "15", "--max-time", "900", "--retry", "3",
                        "--retry-delay", "2", "--output", str(partial), url], check=True)
    else:
        with urllib.request.urlopen(url, timeout=180) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
    if sha(partial) != expected:
        raise ValueError(f"Downloaded public asset checksum mismatch: {path.name}")
    partial.replace(path)
    if cached and not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary_cache = Path(str(cached) + ".partial")
        shutil.copyfile(path, temporary_cache)
        temporary_cache.replace(cached)
    print("Verified public asset:", path.name, retrieval, flush=True)
    return retrieval


def public_release(name, version):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=90) as response:
        return json.load(response)["urls"]


def wheel_identity(path):
    with zipfile.ZipFile(path) as wheel:
        metadata = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("Wheel must contain exactly one distribution")
        data = email.message_from_bytes(wheel.read(metadata[0]))
    return data["Name"], data["Version"]


def check_source_wheel(wheel, archive):
    with tarfile.open(archive, "r:gz") as source:
        original = {entry.name.split("/", 1)[1]: source.extractfile(entry).read()
                    for entry in source.getmembers() if entry.isfile() and "/" in entry.name
                    and entry.name.split("/", 1)[1].startswith("sweagent/") and entry.name.endswith(".py")}
    # This non-package experiment is omitted by the unchanged pinned setuptools build.
    original.pop("sweagent/agent/extra/shell_agent.py", None)
    with zipfile.ZipFile(wheel) as bundle:
        built = {name: bundle.read(name) for name in bundle.namelist()
                 if name.startswith("sweagent/") and name.endswith(".py")}
    if not original or built != original:
        raise ValueError("Built SWE-agent Python files differ from fixed public source")


def merge_identical_tree(source, destination):
    """Merge fixed assets without replacing unknown entries or following target links."""
    source, destination = Path(source), Path(destination)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("Refusing conflicting asset directory: " + str(destination))
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_symlink():
            link = os.readlink(entry)
            if target.is_symlink() and os.readlink(target) == link:
                continue
            if target.exists() or target.is_symlink():
                raise ValueError("Refusing conflicting asset symlink: " + str(target))
            target.symlink_to(link)
        elif entry.is_dir():
            merge_identical_tree(entry, target)
        elif entry.is_file():
            if target.is_symlink() or (target.exists() and (not target.is_file() or sha(target) != sha(entry))):
                raise ValueError("Refusing conflicting asset file: " + str(target))
            if not target.exists():
                shutil.copy2(entry, target)
        else:
            raise ValueError("Unsupported public asset entry: " + str(entry))


class Bundle:
    def __init__(self, args):
        self.args = args
        self.root = args.work_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_dir = args.rock_root.resolve() / "examples/tinker_quick_start"
        self.assets = self.root / "assets"
        self.assets.mkdir(exist_ok=True)
        self.manifest_path = self.root / "bundle-manifest.json"
        self.manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {"revision": COMMIT, "files": {}, "completed_stages": []}
        if self.manifest.get("revision") != COMMIT:
            raise ValueError("Existing bundle uses a different source revision")
        for relative, record in self.manifest["files"].items():
            path = self.root / relative
            if not path.is_file() or sha(path) != record["sha256"]:
                raise ValueError(f"Recorded asset missing or modified: {relative}")
        self.env = dict(os.environ, PIP_CONFIG_FILE=os.devnull, PIP_INDEX_URL=args.index_url,
                        PIP_EXTRA_INDEX_URL="", PIP_NO_INDEX="0", PYTHONNOUSERSITE="1", PYTHON_DOTENV_DISABLED="1")
        self.env.pop("PYTHONPATH", None)
        self.env.pop("PYTHONHOME", None)
        self.env.pop("PIP_FIND_LINKS", None)
        self.python = self.root / "build-venv/bin/python"

    def record(self, path, *, flush=True, **source):
        relative = str(Path(path).relative_to(self.root))
        previous = self.manifest["files"].get(relative, {})
        if "retrieval" in source:
            source["first_retrieval"] = previous.get("first_retrieval", previous.get("retrieval", source["retrieval"]))
        self.manifest["files"][relative] = {"sha256": sha(path), "size": Path(path).stat().st_size, **source}
        if flush:
            write_json(self.manifest_path, self.manifest)

    def call(self, arguments):
        subprocess.run([str(x) for x in arguments], env=self.env, check=True)

    def pip(self, *arguments):
        self.call([self.python, "-m", "pip", *arguments, "--index-url", self.args.index_url])

    def sources(self):
        for name, (url, expected) in ASSETS.items():
            path = self.assets / name
            retrieval = fetch(url, path, expected, self.args.download_cache)
            self.record(path, source_url=url, retrieval=retrieval)
        cache = self.assets / "tiktoken-cache"
        records = []
        for encoding, expected in ENCODINGS.items():
            url = f"https://openaipublic.blob.core.windows.net/encodings/{encoding}.tiktoken"
            filename = hashlib.sha1(url.encode()).hexdigest()
            path = cache / filename
            retrieval = fetch(url, path, expected, self.args.download_cache)
            self.record(path, source_url=url, retrieval=retrieval)
            records.append({"encoding": encoding, "url": url, "sha256": expected, "cache_filename": filename})
        write_json(cache / "manifest.json", {"files": records})
        self.record(cache / "manifest.json", generated=True)
        self.source_manifest()

    def source_manifest(self, wheel=None):
        path = self.assets / "swe-agent-source.json"
        record = {"source": ASSETS["swe-agent-source.tar.gz"][0], "revision": COMMIT,
                  "archive": "swe-agent-source.tar.gz", "sha256": SOURCE_SHA, "extras": False}
        if wheel:
            record["wheel"] = {"filename": Path(wheel).name, "sha256": sha(wheel), "size": Path(wheel).stat().st_size,
                               "build": "unchanged fixed public source; local CP312 build"}
        elif path.exists():
            previous = json.loads(path.read_text())
            if previous.get("wheel"):
                record["wheel"] = previous["wheel"]
        write_json(path, record)
        self.record(path, generated=True)

    def wheelhouse(self, path):
        path.mkdir(parents=True, exist_ok=True)
        pins = dict(SDIST_BUILD)
        lock = self.source_dir / "public_bundle.requirements.txt"
        requested = [line.split("==") for line in lock.read_text().splitlines() if line and not line.startswith("#")]
        def source_only(item):
            name, version = item
            release = public_release(name, version)
            return (name, version) if not any(entry["packagetype"] == "bdist_wheel" for entry in release) else None
        with ThreadPoolExecutor(max_workers=8) as pool:
            for item in pool.map(source_only, requested):
                if item:
                    pins[item[0]] = item[1]
        for name, version in pins.items():
            release = public_release(name, version)
            sdist = next(item for item in release if item["packagetype"] == "sdist")
            source = self.assets / "pypi-sources" / sdist["filename"]
            retrieval = fetch(sdist["url"], source, sdist["digests"]["sha256"], self.args.download_cache)
            self.record(source, source_url=sdist["url"], retrieval=retrieval)
            candidates = [wheel for wheel in path.glob("*.whl") if wheel_identity(wheel)[0].lower().replace("_", "-") == name.lower().replace("_", "-")]
            for wheel in candidates:
                record = self.manifest["files"].get(str(wheel.relative_to(self.root)), {})
                if record.get("source_sha256") != sdist["digests"]["sha256"] or record.get("sha256") != sha(wheel):
                    raise ValueError(f"Refusing unrecorded native/source-built wheel: {wheel.name}")
            if not candidates:
                self.pip("wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", path, source)
            for wheel in path.glob("*.whl"):
                if wheel_identity(wheel)[0].lower().replace("_", "-") == name.lower().replace("_", "-"):
                    self.record(wheel, built_from_sdist=sdist["url"], source_sha256=sdist["digests"]["sha256"])

    def verify_pypi_wheels(self, directory, local_names=()):
        def verify(path):
            relative = str(path.relative_to(self.root))
            if relative in self.manifest["files"] and "built_from_sdist" in self.manifest["files"][relative]:
                return path, self.manifest["files"][relative]
            name, version = wheel_identity(path)
            if name.lower().replace("_", "-") in local_names:
                return path, None
            releases = public_release(name, version)
            matches = [item for item in releases if item["filename"] == path.name]
            if len(matches) != 1 or matches[0]["digests"]["sha256"] != sha(path):
                raise ValueError(f"Wheel is not the official PyPI file: {path.name}")
            return path, {"source_url": matches[0]["url"], "official_pypi_sha256": matches[0]["digests"]["sha256"]}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for path, record in pool.map(verify, sorted(directory.glob("*.whl"))):
                if record is not None:
                    self.record(path, **{key: value for key, value in record.items() if key not in ("sha256", "size")})

    def contexts(self):
        agent, outer = self.root / "agent-context", self.root / "outer-context"
        agent.mkdir(exist_ok=True)
        outer.mkdir(exist_ok=True)
        for name in ("Dockerfile.agent", "sweagent-wrapper", "prepare_agent_tools.py", "public-sweagent.yaml"):
            shutil.copy2(self.source_dir / name, agent / name)
        for name in ("Dockerfile", "public_swe_agent.py"):
            shutil.copy2(self.source_dir / name, outer / name)
        for src, dst in ((self.root / "agent-wheels", agent / "agent-wheels"),
                         (self.root / "tool-wheels", agent / "tool-wheels"),
                         (self.assets / "tiktoken-cache", agent / "tiktoken-cache")):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        for name in ("swe-agent-source.tar.gz", "swe-agent-source.json"):
            shutil.copy2(self.assets / name, agent / name)
        # Extract into a newly owned directory; never remove or overwrite an older staging tree.
        with tempfile.TemporaryDirectory(prefix=".extract-public-", dir=self.root) as temporary:
            staging = Path(temporary)
            for filename in (PBS_NAME, "docker-29.8.2.tgz"):
                with tarfile.open(self.assets / filename) as archive:
                    archive.extractall(staging, filter="data")
            merge_identical_tree(staging / "python", agent / "python312")
            merge_identical_tree(staging / "docker", outer / "docker")
        shutil.copy2(self.assets / "docker-compose-linux-x86_64", outer / "docker-compose-linux-x86_64")
        (outer / "docker-compose-linux-x86_64").chmod(0o755)
        for context in (agent, outer):
            for path in context.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    self.record(path, flush=False, docker_copy_input=True)
        self.manifest["pending_external_inputs"] = [
            "build isolated agent runtime",
            "copy sweagent-runtime.tar.gz -> outer-context (not yet built)",
            "Dockerfile apt: first build needs public Debian repositories",
        ]
        write_json(self.manifest_path, self.manifest)
