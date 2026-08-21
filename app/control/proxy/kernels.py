"""Proxy kernel discovery and installation helpers."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from app.platform.config.snapshot import get_config
from app.platform.paths import data_path


KERNEL_NATIVE = "native"
KERNEL_XRAY = "xray"
KERNEL_SING_BOX = "sing-box"
KERNEL_MIHOMO = "mihomo"
KERNELS = (KERNEL_XRAY, KERNEL_SING_BOX, KERNEL_MIHOMO)
MAX_KERNEL_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_KERNEL_RELEASE_BYTES = 2 * 1024 * 1024
_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".gz")


@dataclass(frozen=True)
class KernelSpec:
    kernel: str
    display_name: str
    repository: str
    binary_name: str
    environment_variable: str


@dataclass(frozen=True)
class KernelAsset:
    name: str
    url: str
    size: int = 0


KERNEL_SPECS = {
    KERNEL_XRAY: KernelSpec(
        KERNEL_XRAY, "Xray", "XTLS/Xray-core", "xray", "XRAY_BINARY_PATH"
    ),
    KERNEL_SING_BOX: KernelSpec(
        KERNEL_SING_BOX,
        "sing-box",
        "SagerNet/sing-box",
        "sing-box",
        "SING_BOX_BINARY_PATH",
    ),
    KERNEL_MIHOMO: KernelSpec(
        KERNEL_MIHOMO,
        "Mihomo",
        "MetaCubeX/mihomo",
        "mihomo",
        "MIHOMO_BINARY_PATH",
    ),
}


@dataclass(frozen=True)
class KernelStatus:
    kernel: str
    display_name: str
    installed: bool
    configured: bool
    path: str = ""
    source: str = ""
    message: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "kernel": self.kernel,
            "name": self.display_name,
            "installed": self.installed,
            "configured": self.configured,
            "path": self.path,
            "source": self.source,
            "message": self.message,
        }


def normalize_kernel(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "auto"}:
        return ""
    if normalized in {"singbox", "sing_box", "sing-box"}:
        return KERNEL_SING_BOX
    if normalized in {"clash", "clash-meta", "mihomo"}:
        return KERNEL_MIHOMO
    if normalized in {"xray", "x-ray"}:
        return KERNEL_XRAY
    if normalized == KERNEL_NATIVE:
        return KERNEL_NATIVE
    return normalized


def current_platform() -> tuple[str, str]:
    system = platform.system().lower()
    if system.startswith("win"):
        system = "windows"
    elif system.startswith("darwin") or system.startswith("mac"):
        system = "darwin"
    elif system.startswith("linux"):
        system = "linux"
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64", "x64"}:
        machine = "amd64"
    elif machine in {"aarch64", "arm64"}:
        machine = "arm64"
    elif machine in {"x86", "i386", "i686", "386"}:
        machine = "386"
    return system, machine


class ProxyKernelManager:
    """Find, inspect and install Xray, sing-box and Mihomo."""

    def __init__(self) -> None:
        self._download_locks: dict[str, asyncio.Lock] = {}

    @property
    def root(self) -> Path:
        configured = get_config().get_str("proxy.kernels.directory", "").strip()
        if configured:
            path = Path(configured).expanduser()
            return path if path.is_absolute() else Path.cwd() / path
        return data_path("proxy-kernels")

    def statuses(self) -> list[KernelStatus]:
        return [self.status(kernel) for kernel in KERNELS]

    def status(self, kernel: str) -> KernelStatus:
        spec = KERNEL_SPECS.get(normalize_kernel(kernel))
        if spec is None:
            return KernelStatus(normalize_kernel(kernel), str(kernel), False, False, message="不支持的代理内核")
        configured = self._configured_path(spec)
        resolved = self.resolve_binary(spec.kernel)
        if resolved:
            source = "configured" if configured and resolved == Path(configured).resolve() else "local"
            return KernelStatus(spec.kernel, spec.display_name, True, bool(configured), str(resolved), source, "已就绪")
        message = f"配置路径不可用：{configured}" if configured else "未找到可执行文件"
        return KernelStatus(spec.kernel, spec.display_name, False, bool(configured), message=message)

    def resolve_binary(self, kernel: str) -> Path | None:
        spec = KERNEL_SPECS.get(normalize_kernel(kernel))
        if spec is None:
            return None
        system, machine = current_platform()
        names = [spec.binary_name]
        if system == "windows":
            names.insert(0, f"{spec.binary_name}.exe")
        candidates: list[Path] = []
        configured = self._configured_path(spec)
        if configured:
            path = Path(configured).expanduser()
            candidates.extend(path / name for name in names) if path.is_dir() else candidates.append(path)
        install_dir = self.root / spec.kernel / f"{system}-{machine}"
        candidates.extend(install_dir / name for name in names)
        candidates.extend(self.root / spec.kernel / name for name in names)
        project_root = Path(__file__).resolve().parents[3]
        candidates.extend(project_root / "bin" / f"{system}-{machine}" / spec.kernel / name for name in names)
        candidates.extend(project_root / "bin" / spec.kernel / name for name in names)
        if system == "windows" and spec.kernel == KERNEL_MIHOMO:
            appdata_roots = [
                os.getenv("APPDATA", ""),
                os.getenv("LOCALAPPDATA", ""),
            ]
            for appdata in appdata_roots:
                if not appdata:
                    continue
                base = Path(appdata) / "mihomo-party"
                candidates.extend(
                    base / relative / name
                    for relative in (Path(), Path("core"), Path("resources"))
                    for name in names
                )
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
        for candidate in candidates:
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def _configured_path(self, spec: KernelSpec) -> str:
        value = get_config().get_str(f"proxy.kernels.{spec.kernel}.path", "").strip()
        return str(Path(value).expanduser()) if value else os.getenv(spec.environment_variable, "").strip()

    async def ensure(self, kernel: str, *, auto_download: bool = False) -> Path:
        normalized = normalize_kernel(kernel)
        if normalized not in KERNEL_SPECS:
            raise RuntimeError(f"不支持的代理内核：{kernel}")
        existing = self.resolve_binary(normalized)
        if existing:
            return existing
        if not auto_download:
            raise RuntimeError(f"{KERNEL_SPECS[normalized].display_name} 内核未安装，请先准备内核")
        lock = self._download_locks.setdefault(normalized, asyncio.Lock())
        async with lock:
            existing = self.resolve_binary(normalized)
            if existing:
                return existing
            return await asyncio.to_thread(self._download_sync, normalized)

    async def download(self, kernel: str) -> KernelStatus:
        await self.ensure(kernel, auto_download=True)
        return self.status(kernel)

    def _platform_install_dir(self, spec: KernelSpec) -> Path:
        system, machine = current_platform()
        return self.root / spec.kernel / f"{system}-{machine}"

    def _download_timeout(self) -> float:
        return max(10.0, get_config().get_float("proxy.kernels.download_timeout_sec", 180.0))

    def _download_proxy(self) -> str:
        return get_config().get_str("proxy.kernels.download_proxy_url", "").strip()

    def _release_version(self, spec: KernelSpec) -> str:
        version = get_config().get_str(
            f"proxy.kernels.{spec.kernel}.version", "latest"
        ).strip()
        if not version or version.lower() == "latest":
            return "latest"
        return version if version.lower().startswith("v") else f"v{version}"

    def _open_url(self, url: str, *, accept: str = ""):
        headers = {
            "User-Agent": "grok2api-proxy-kernel/1.0",
            "Accept": accept or "*/*",
        }
        request = Request(url, headers=headers)
        proxy = self._download_proxy()
        if proxy:
            opener = build_opener(ProxyHandler({"http": proxy, "https": proxy}))
        else:
            opener = build_opener()
        return opener.open(request, timeout=self._download_timeout())

    def _download_uses_socks(self) -> bool:
        return urlparse(self._download_proxy()).scheme.lower().startswith("socks")

    def _curl_get_bytes(self, url: str, *, accept: str = "") -> bytes:
        from curl_cffi import requests as curl_requests

        headers = {
            "User-Agent": "grok2api-proxy-kernel/1.0",
            "Accept": accept or "*/*",
        }
        response = curl_requests.get(
            url,
            headers=headers,
            proxy=self._download_proxy(),
            timeout=self._download_timeout(),
        )
        response.raise_for_status()
        return response.content

    def _curl_download(self, url: str, target: Path) -> None:
        from curl_cffi import requests as curl_requests

        headers = {
            "User-Agent": "grok2api-proxy-kernel/1.0",
            "Accept": "*/*",
        }
        session = curl_requests.Session()
        try:
            response = session.get(
                url,
                headers=headers,
                proxy=self._download_proxy(),
                timeout=self._download_timeout(),
                stream=True,
            )
            response.raise_for_status()
            remaining = MAX_KERNEL_ARCHIVE_BYTES
            with target.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise RuntimeError("代理内核压缩包超过 512 MB 限制")
                    output.write(chunk)
        finally:
            session.close()

    def _fetch_release(self, spec: KernelSpec) -> dict[str, Any]:
        version = self._release_version(spec)
        if version == "latest":
            url = f"https://api.github.com/repos/{spec.repository}/releases/latest"
        else:
            url = f"https://api.github.com/repos/{spec.repository}/releases/tags/{version}"
        try:
            if self._download_uses_socks():
                body = self._curl_get_bytes(url, accept="application/vnd.github+json")
            else:
                with self._open_url(url, accept="application/vnd.github+json") as response:
                    body = response.read(MAX_KERNEL_RELEASE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"查询 {spec.display_name} Release 失败：{exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"查询 {spec.display_name} Release 失败：{exc}") from exc
        if len(body) > MAX_KERNEL_RELEASE_BYTES:
            raise RuntimeError("代理内核 Release 响应过大")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("代理内核 Release 响应不是有效 JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
            message = payload.get("message", "未知响应") if isinstance(payload, dict) else "未知响应"
            raise RuntimeError(f"{spec.display_name} Release 不可用：{message}")
        return payload

    def _download_sync(self, kernel: str) -> Path:
        spec = KERNEL_SPECS[kernel]
        system, machine = current_platform()
        release = self._fetch_release(spec)
        asset = self._select_asset(spec, release.get("assets", []), system, machine)
        install_dir = self._platform_install_dir(spec)
        install_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{kernel}-", dir=str(install_dir)) as temp_dir:
            archive_path = Path(temp_dir) / asset.name
            self._download_asset(asset, archive_path)
            extract_dir = Path(temp_dir) / "extract"
            extract_dir.mkdir()
            self._extract_archive(archive_path, extract_dir)
            binary = self._find_extracted_binary(extract_dir, spec, system)
            target_name = f"{spec.binary_name}.exe" if system == "windows" else spec.binary_name
            target = install_dir / target_name
            staged = Path(temp_dir) / target_name
            shutil.copy2(binary, staged)
            self._make_executable(staged)
            os.replace(staged, target)
        return target.resolve()

    def _download_asset(self, asset: KernelAsset, target: Path) -> None:
        try:
            if self._download_uses_socks():
                self._curl_download(asset.url, target)
            else:
                with self._open_url(asset.url) as response, target.open("wb") as output:
                    remaining = MAX_KERNEL_ARCHIVE_BYTES
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise RuntimeError("代理内核压缩包超过 512 MB 限制")
                        output.write(chunk)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"下载代理内核失败：{exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"下载代理内核失败：{exc}") from exc

    @staticmethod
    def _make_executable(path: Path) -> None:
        try:
            mode = path.stat().st_mode
            path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as exc:
            raise RuntimeError(f"设置代理内核可执行权限失败：{path}") from exc

    @staticmethod
    def _safe_child_path(root: Path, member_name: str) -> Path:
        if not member_name or "\x00" in member_name:
            raise RuntimeError("代理内核压缩包包含无效路径")
        root_resolved = root.resolve()
        target = (root / member_name).resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError as exc:
            raise RuntimeError("代理内核压缩包包含越界路径") from exc
        return target

    def _extract_archive(self, archive: Path, destination: Path) -> None:
        name = archive.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    target = self._safe_child_path(destination, member.filename)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with source.open(member) as input_file, target.open("wb") as output:
                        shutil.copyfileobj(input_file, output, length=1024 * 1024)
            return
        if name.endswith((".tar.gz", ".tgz")):
            with tarfile.open(archive, "r:gz") as source:
                for member in source.getmembers():
                    if member.issym() or member.islnk():
                        raise RuntimeError("代理内核压缩包不允许包含符号链接")
                    target = self._safe_child_path(destination, member.name)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise RuntimeError("代理内核压缩包包含不支持的文件类型")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = source.extractfile(member)
                    if extracted is None:
                        raise RuntimeError("代理内核压缩包文件读取失败")
                    with extracted, target.open("wb") as output:
                        shutil.copyfileobj(extracted, output, length=1024 * 1024)
            return
        if name.endswith(".gz"):
            target = destination / archive.name[:-3]
            target = self._safe_child_path(destination, target.name)
            with gzip.open(archive, "rb") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            return
        raise RuntimeError(f"不支持的代理内核压缩格式：{archive.name}")

    @staticmethod
    def _find_extracted_binary(root: Path, spec: KernelSpec, system: str) -> Path:
        names = {spec.binary_name.lower()}
        if system == "windows":
            names.add(f"{spec.binary_name}.exe")
        matches = []
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            candidate_name = candidate.name.lower()
            if candidate_name in names or any(
                candidate_name.startswith(f"{name}-") for name in names
            ):
                matches.append(candidate)
        if not matches:
            raise RuntimeError(f"压缩包中未找到 {spec.display_name} 可执行文件")
        return sorted(matches, key=lambda item: len(item.parts))[0]

    @staticmethod
    def _select_asset(
        spec: KernelSpec, assets: list[Any], system: str, machine: str
    ) -> KernelAsset:
        os_tokens = {
            "windows": ("windows", "win"),
            "linux": ("linux",),
            "darwin": ("darwin", "macos", "osx"),
        }.get(system, (system,))
        arch_patterns = {
            "amd64": (r"(?:^|[-_.])amd64(?:$|[-_.])", r"(?:^|[-_.])x86_64(?:$|[-_.])", r"(?:^|[-_.])64(?:$|[-_.])"),
            "arm64": (r"(?:^|[-_.])arm64(?:$|[-_.])", r"(?:^|[-_.])aarch64(?:$|[-_.])", r"(?:^|[-_.])arm64-v8a(?:$|[-_.])"),
            "386": (r"(?:^|[-_.])386(?:$|[-_.])", r"(?:^|[-_.])i386(?:$|[-_.])", r"(?:^|[-_.])32(?:$|[-_.])"),
        }.get(machine, (re.escape(machine),))
        candidates: list[tuple[int, KernelAsset]] = []
        for raw in assets:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", "")).strip()
            url = str(raw.get("browser_download_url", "")).strip()
            lower = name.lower()
            if not name or not url or not lower.endswith(_ARCHIVE_SUFFIXES):
                continue
            if any(token in lower for token in ("sha256", "checksum", ".sig", ".asc", "source", "deb", "rpm")):
                continue
            if not any(token in lower for token in os_tokens):
                continue
            if not any(re.search(pattern, lower) for pattern in arch_patterns):
                continue
            score = 0
            if spec.kernel == KERNEL_XRAY and lower.endswith(".zip"):
                score += 10
            if spec.kernel == KERNEL_SING_BOX:
                score += 8 if (system == "windows" and lower.endswith(".zip")) else 4
            if spec.kernel == KERNEL_MIHOMO:
                score += 8 if "compatible" in lower else 0
                score += 4 if lower.endswith((".zip", ".gz")) else 0
            size = int(raw.get("size") or 0)
            candidates.append((score, KernelAsset(name, url, size)))
        if not candidates:
            raise RuntimeError(f"官方 Release 中没有匹配 {system}/{machine} 的 {spec.display_name} 资产")
        candidates.sort(key=lambda item: (item[0], -len(item[1].name)), reverse=True)
        return candidates[0][1]
