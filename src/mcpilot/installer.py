"""Explicit, version-pinned package installation into per-package environments.

This is dependency isolation, not an operating-system sandbox. Local packages
remain trusted executable code and must pass host policy before this module is
called. Python dependencies must form a complete explicitly pinned wheel set;
npm records its resolved dependency graph in package-lock.json on first install.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from packaging.version import InvalidVersion, Version

from .models import PackageSpec


class InstallationError(RuntimeError):
    """A sanitized installation failure without subprocess output or secrets."""


def sanitized_environment(directory: Path, supplied: dict[str, str] | None = None) -> dict[str, str]:
    """Inherit runtime essentials only; credentials must be passed explicitly."""
    home = directory / "home"
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "LOGNAME": "mcpilot",
        "USER": "mcpilot",
        "SHELL": "/bin/sh",
        "TERM": "dumb",
        "LANG": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "npm_config_userconfig": os.devnull,
        "npm_config_cache": str(home / ".npm"),
    }
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, value in (supplied or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value or "=" in key:
            raise InstallationError("Invalid subprocess environment")
        env[key] = value
    return env


_PYTHON_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_NODE_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z")
_NODE_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z")
_ENTRYPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _pinned(name: str, version: str, runtime: str) -> str:
    if runtime == "python":
        if not _PYTHON_NAME.fullmatch(name):
            raise InstallationError("Python package name is invalid")
        try:
            parsed = Version(version)
        except InvalidVersion:
            raise InstallationError("Python package version must be exact") from None
        if parsed.local is not None or not version or version != version.strip():
            raise InstallationError("Python package version must be exact and public")
        return f"{name}=={version}"
    if not _NODE_NAME.fullmatch(name) or not _NODE_VERSION.fullmatch(version):
        raise InstallationError("Node package must have an exact semantic version")
    return f"{name}@{version}"


def _requirements(spec: PackageSpec) -> tuple[str, ...]:
    result = [_pinned(spec.name, spec.version, spec.runtime)]
    for dependency in spec.dependencies:
        if spec.runtime == "python":
            if dependency.count("==") != 1:
                raise InstallationError("Every Python dependency must use an exact == version")
            name, version = dependency.split("==", 1)
        else:
            if "@" not in dependency[1:]:
                raise InstallationError("Every Node dependency must use an exact @version")
            name, version = dependency.rsplit("@", 1)
        result.append(_pinned(name, version, spec.runtime))
    if spec.entrypoint is not None and not _ENTRYPOINT.fullmatch(spec.entrypoint):
        raise InstallationError("Entrypoint must be a plain executable name")
    if spec.runtime == "python" and not spec.entrypoint:
        if len(spec.args) < 2 or spec.args[0] != "-m" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", spec.args[1]):
            raise InstallationError("Python packages need an explicit entrypoint or -m module arguments")
    if spec.runtime == "node" and not spec.entrypoint:
        raise InstallationError("Node packages need an explicit executable entrypoint")
    if any("\x00" in arg for arg in spec.args):
        raise InstallationError("Package arguments contain an invalid character")
    return tuple(result)


@asynccontextmanager
async def _file_lock(path: Path, timeout: float) -> AsyncIterator[None]:
    """Cross-process lock. Lock files contain no credentials and survive crashes."""
    handle = open(path, "a+b")
    os.chmod(path, 0o600)
    acquired = False
    try:
        async with asyncio.timeout(timeout):
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        if not handle.read(1):
                            handle.write(b"0")
                            handle.flush()
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except (BlockingIOError, PermissionError):
                    await asyncio.sleep(0.05)
        yield
    except TimeoutError:
        raise InstallationError("Timed out waiting for package installation") from None
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class PackageInstaller:
    def __init__(self, directory: Path | str, *, timeout: float = 180.0) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.timeout = timeout
        self._locks: dict[str, asyncio.Lock] = {}

    async def _run(self, command: Sequence[str], directory: Path) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=directory,
                env=sanitized_environment(directory),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
        except (OSError, ValueError):
            raise InstallationError("Package installer executable is unavailable") from None
        try:
            async with asyncio.timeout(self.timeout):
                code = await process.wait()
        except BaseException:
            if process.returncode is None:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise
        if code != 0:
            raise InstallationError("Package installation failed; verify pinned packages and runtime availability")

    @staticmethod
    def _command(spec: PackageSpec, destination: Path) -> tuple[str, ...]:
        if spec.runtime == "python":
            binary = destination / ("Scripts" if os.name == "nt" else "bin")
            executable = spec.entrypoint or ("python.exe" if os.name == "nt" else "python")
            return (str(binary / executable), *spec.args)
        return (str(destination / "node_modules" / ".bin" / str(spec.entrypoint)), *spec.args)

    async def prepare(self, spec: PackageSpec) -> tuple[str, ...]:
        requirements = _requirements(spec)
        payload = spec.model_dump(mode="json")
        # The interpreter participates in the key: do not reuse an environment
        # created for a different Python installation after host upgrades.
        identity = {"package": payload, "python": sys.executable, "version": list(sys.version_info[:3])}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:32]
        destination = self.directory / digest
        lock = self._locks.setdefault(digest, asyncio.Lock())
        async with lock:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            async with _file_lock(self.directory / f"{digest}.lock", self.timeout):
                marker = destination / ".mcpilot-installed.json"
                command = self._command(spec, destination)
                if marker.is_file() and Path(command[0]).is_file():
                    return command
                if destination.exists():
                    await asyncio.to_thread(shutil.rmtree, destination)
                destination.mkdir(mode=0o700)
                try:
                    if spec.runtime == "python":
                        await self._run((sys.executable, "-m", "venv", str(destination)), destination)
                        python = destination / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                        await self._run((
                            str(python), "-m", "pip", "--isolated", "--disable-pip-version-check",
                            "install", "--no-input", "--no-deps", "--only-binary=:all:",
                            "--index-url", "https://pypi.org/simple", *requirements,
                        ), destination)
                    else:
                        npm = shutil.which("npm")
                        if npm is None:
                            raise InstallationError("Node installation requires npm on the host")
                        await self._run((
                            npm, "install", "--prefix", str(destination), "--save-exact",
                            "--ignore-scripts", "--no-audit", "--no-fund", "--package-lock",
                            "--registry=https://registry.npmjs.org", *requirements,
                        ), destination)
                    if not Path(command[0]).is_file():
                        raise InstallationError("Installed package does not provide its approved entrypoint")
                    marker.write_text(json.dumps(identity, sort_keys=True), encoding="utf-8")
                    marker.chmod(0o600)
                except BaseException:
                    await asyncio.to_thread(shutil.rmtree, destination, True)
                    raise
                return command
