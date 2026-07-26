"""Per-KB read-only guard.

The KB-level ``.openkb/config.yaml`` may set ``read_only: true`` to disable
mutating operations on that KB — currently the three CLI commands
(``add``/``remove``/``recompile``) and the matching HTTP endpoints. Wiki
page edits/deletes are intentionally NOT covered here (compiled-artifact
edits are out of scope for this toggle).

The CLI and HTTP entry points both call :func:`enforce_not_read_only` after
they have resolved the KB directory but BEFORE any work happens, and raise
their respective error type (``click.ClickException`` / ``HTTPException
(403)``) so callers see a clear failure rather than a partial mutation.

Note: ``read_only`` is KB-only — ``global.yaml`` is deliberately NOT
honoured. The resolver (:func:`openkb.config.resolve_read_only`) is keyed
on the KB's own config, so flipping it requires editing the KB on disk.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _load_kb_config(kb_dir: Path) -> dict:
    """Load a KB's ``.openkb/config.yaml`` as a dict.

    Deliberately NOT delegated to :func:`openkb.config.load_config` to keep
    this module import-order independent of ``openkb.config`` (which
    re-exports this resolver — that would form a circular import). The
    file is a tiny per-KB YAML; this loader mirrors the tolerant subset
    of :func:`openkb.config.load_config` (a non-mapping or absent file
    becomes ``{}``).
    """
    config_path = kb_dir / ".openkb" / "config.yaml"
    if not config_path.exists():
        return {}
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def resolve_read_only(config: dict) -> bool:
    """Resolve the optional ``read_only:`` key — a per-KB safety toggle that
    disables the mutating CLI commands (``add``/``remove``/``recompile``) and
    the matching HTTP endpoints when ``True``.

    Accepts YAML booleans (``true``/``false``) and the common truthy/falsy
    string aliases (``"true"``/``"yes"``/``"on"``/``"1"`` and their lower-case
    counterparts), so a hand-edited config stays usable. ``0``/``1`` are
    accepted as ints. Anything else — a non-boolean scalar (``"maybe"``), a
    number outside ``{0, 1}``, a list, a mapping — logs a warning and falls
    back to ``False`` so a typo never silently LOCKS the KB behind a
    misconfiguration. Absent / explicit ``null`` / empty string → ``False``
    (silent — these are the normal "not configured" case, matching how
    :func:`openkb.config.resolve_concurrency` treats an explicit ``None``).

    Note: ``read_only`` is intentionally NOT in :data:`GLOBAL_SCALAR_KEYS` —
    it is a per-KB gate, not a server-wide default. ``global.yaml`` is
    ignored; only ``<kb_dir>/.openkb/config.yaml`` matters.

    Lives in this module rather than :mod:`openkb.config` so the resolver
    sits beside the enforcement code that consumes it, and so
    :mod:`openkb.config` stays under the 800-line ceiling
    (see ``tests/test_file_size.py``).
    """
    import logging

    logger = logging.getLogger(__name__)
    if "read_only" not in config:
        return False
    raw = config["read_only"]
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        # Accept 0/1, reject any other integer so a typo can't silently
        # lock the KB behind e.g. ``read_only: 2``.
        if raw in (0, 1):
            return bool(raw)
        logger.warning(
            "config: 'read_only' must be true/false (or 0/1), got %r — ignoring it.",
            raw,
        )
        return False
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("", "false", "off", "0"):
            return False
        if s in ("true", "yes", "on", "1"):
            return True
        logger.warning(
            "config: 'read_only' must be true/false (or 0/1), got %r — ignoring it.",
            raw,
        )
        return False
    logger.warning(
        "config: 'read_only' must be true/false (or 0/1), got %s — ignoring it.",
        type(raw).__name__,
    )
    return False


class ReadOnlyError(Exception):
    """Raised when an operation is rejected by a KB's ``read_only: true`` flag.

    The CLI catches this and re-raises as ``click.ClickException`` (exit
    code non-zero); the HTTP layer re-raises as ``HTTPException(403)``.
    The message names the operation so logs and 403 bodies stay informative.
    """

    def __init__(self, kb_name: str, op: str) -> None:
        super().__init__(
            f"KB {kb_name!r} is read-only: {op} is disabled "
            "(set `read_only: false` in .openkb/config.yaml to re-enable)."
        )
        self.kb_name = kb_name
        self.op = op


def enforce_not_read_only(kb_dir: Path, op: str) -> None:
    """Raise :class:`ReadOnlyError` if the KB's config.yaml has ``read_only: true``.

    A missing config (a KB created before this toggle existed, or one that
    simply has no config file yet) is treated as NOT read-only — the toggle
    is opt-in.
    """
    config = _load_kb_config(kb_dir)
    if resolve_read_only(config):
        raise ReadOnlyError(kb_dir.name or str(kb_dir), op)