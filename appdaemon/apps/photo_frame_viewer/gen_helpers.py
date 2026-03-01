"""Pure-Python helpers for photo frame generation management.

No AppDaemon or filesystem dependencies -- safe for unit testing.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import quote


def basename(path: str) -> str:
    """Extract the filename from a POSIX-style path."""
    try:
        return str(PurePosixPath(str(path))).split("/")[-1] or ""
    except Exception:
        return ""


def compute_fingerprint(
    file_paths: list[str],
    *,
    file_stats: dict[str, tuple[int, float]] | None = None,
) -> str:
    """Stable fingerprint from a list of source file paths.

    When *file_stats* is provided (mapping path → (size, mtime)), the
    fingerprint includes size and mtime so content changes are detected
    even when filenames stay the same.

    Returns a hex SHA-256 digest.
    """
    entries: list[str] = []
    for p in sorted(file_paths, key=basename):
        name = basename(p)
        if not name:
            continue
        if file_stats and p in file_stats:
            size, mtime = file_stats[p]
            entries.append(f"{name}:{size}:{mtime}")
        else:
            entries.append(name)
    payload = "\n".join(entries).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_label_maps(
    file_paths: list[str],
    options_max: int = 100,
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Build friendly labels from file paths, disambiguating duplicates.

    Returns (labels, label_to_path, path_to_label).
    """
    file_paths = file_paths[:options_max]
    counts: dict[str, int] = {}
    label_to_path: dict[str, str] = {}
    path_to_label: dict[str, str] = {}
    labels: list[str] = []

    for p in file_paths:
        base = basename(p).strip() or "image"
        n = counts.get(base, 0) + 1
        counts[base] = n
        label = base if n == 1 else f"{base} ({n})"
        labels.append(label)
        label_to_path[label] = p
        path_to_label[p] = label

    return labels, label_to_path, path_to_label


def source_paths_to_gen_paths(
    source_paths: list[str],
    gen_id: str,
    live_ha_dir: str = "/config/www/photo-frame/live",
) -> list[str]:
    """Map source paths to their expected location in a generation directory.

    Example:
        /config/www/immich-album/foo.jpg  ->  /config/www/photo-frame/live/2/foo.jpg
    """
    base_dir = f"{live_ha_dir.rstrip('/')}/{gen_id}"
    return [f"{base_dir}/{basename(p)}" for p in source_paths if basename(p)]


def gen_path_to_local_url(
    gen_ha_path: str,
    ha_local_url_base: str = "/local/photo-frame/live",
    gen_id: str = "",
) -> str:
    """Convert a gen HA path to a ``/local/...`` URL with proper encoding.

    If *gen_id* is provided, builds from base + gen_id + basename.
    Otherwise falls back to converting ``/config/www/`` prefix to ``/local/``.
    """
    name = basename(gen_ha_path)
    if not name:
        return f"{ha_local_url_base}/unknown"

    if gen_id:
        return f"{ha_local_url_base.rstrip('/')}/{gen_id}/{quote(name, safe='')}"

    p = str(PurePosixPath(str(gen_ha_path or "").strip()))
    if p.startswith("/config/www/"):
        rel = p[len("/config/www/"):]
        return "/local/" + quote(rel, safe="/")
    return quote(p, safe="/")


_GEN_ID_RE: Optional[re.Pattern[str]] = None


def _gen_id_pattern(ha_local_url_base: str) -> re.Pattern[str]:
    """Compile (and cache) a regex that extracts a gen id from a published URL."""
    global _GEN_ID_RE
    if _GEN_ID_RE is not None:
        return _GEN_ID_RE
    escaped = re.escape(ha_local_url_base.rstrip("/"))
    _GEN_ID_RE = re.compile(rf"^{escaped}/(\d+)/")
    return _GEN_ID_RE


def parse_gen_id_from_url(
    url: str,
    ha_local_url_base: str = "/local/photo-frame/live",
) -> Optional[str]:
    """Extract the generation id from a published local URL.

    Returns the gen id string (e.g. ``"3"``) or ``None`` if the URL
    doesn't match the expected pattern.
    """
    pat = _gen_id_pattern(ha_local_url_base)
    m = pat.match(url or "")
    return m.group(1) if m else None
