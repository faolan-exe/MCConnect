"""The MCDataLink plugin jar that MCConnect offers: download on the admin page and automatic updates (plugin 3.18).

The plugin gets the version and SHA-256 of this jar over its pinned TLS connection (!update) and installs a newer one
into the server's update folder only if the download has exactly this checksum.
"""
import glob
import hashlib
import os
import re
import zipfile

from database import config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VERSION_RE = re.compile(r"^version:\s*'?([0-9][0-9.]*)'?\s*$", re.MULTILINE)
_cache = {}  # path -> (mtime, size, info)


def path():
    """The jar file (MCC_PLUGIN_JAR, or the newest local maven build), or None."""
    if config.PLUGIN_JAR:
        return config.PLUGIN_JAR if os.path.isfile(config.PLUGIN_JAR) else None
    builds = glob.glob(os.path.join(PROJECT_ROOT, "java plugin", "MCDataLink", "target", "MCDataLink-*.jar"))
    return max(builds, key=lambda p: version_key(p.rsplit("-", 1)[-1][:-4])) if builds else None


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def info():
    """{"version", "sha256", "size"} of the jar (cached until the file changes), or None."""
    jar = path()
    if jar is None:
        return None
    stat = os.stat(jar)
    cached = _cache.get(jar)
    if cached and cached[:2] == (stat.st_mtime, stat.st_size):
        return cached[2]
    with open(jar, "rb") as file:
        data = file.read()
    with zipfile.ZipFile(jar) as archive:
        match = _VERSION_RE.search(archive.read("plugin.yml").decode("utf-8"))
    result = {"version": match.group(1) if match else None, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    _cache[jar] = (stat.st_mtime, stat.st_size, result)
    return result


def update_message():
    """!update for the plugin: version|download url|sha256, or None without a jar."""
    jar = info()
    if not jar or not jar["version"]:
        return None
    return (f"!update~{jar['version']}|{config.PUBLIC_SCHEME}://{config.BASE_DOMAIN}/download/MCDataLink.jar|"
            f"{jar['sha256']}")
