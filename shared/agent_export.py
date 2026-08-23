from __future__ import annotations

"""Export approved learned agent definitions as provider-native skill files."""

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import DB_PATH, TGsConfig
from .db import Database
from .db_client import open_database

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider targets
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExportTarget:
    provider_id: str
    project_subdir: str    # relative path under project root
    global_dir: Path       # user-global dir
    layout: str            # skill_dir (slug dir with SKILL.md) or flat_md (slug.md)


_BUILTIN_TARGETS: list[ExportTarget] = [
    ExportTarget(
        provider_id="claude-code",
        project_subdir=".claude/skills",
        global_dir=Path.home() / ".claude" / "skills",
        layout="skill_dir",
    ),
    ExportTarget(
        provider_id="github-copilot-cli",
        project_subdir=".github/agents",
        global_dir=Path.home() / ".copilot" / "agents",
        layout="flat_md",
    ),
    ExportTarget(
        provider_id="codex",
        project_subdir=".codex/skills",
        global_dir=Path.home() / ".agents" / "skills",
        layout="skill_dir",
    ),
    ExportTarget(
        provider_id="cursor",
        project_subdir=".cursor/skills",
        global_dir=Path.home() / ".cursor" / "skills",
        layout="skill_dir",
    ),
    ExportTarget(
        provider_id="opencode",
        project_subdir=".opencode/agent",
        global_dir=Path.home() / ".config" / "opencode" / "agent",
        layout="flat_md",
    ),
    ExportTarget(
        provider_id="antigravity",
        project_subdir=".gemini/config/agents",
        global_dir=Path.home() / ".gemini" / "config" / "agents",
        layout="flat_md",
    ),
]

_TARGET_BY_PROVIDER: dict[str, ExportTarget] = {t.provider_id: t for t in _BUILTIN_TARGETS}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _slugify(value: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "learned-agent"


def _resolve_export_path(root: Path, subdir: str, slug: str, layout: str) -> Path:
    target_dir = root.joinpath(subdir).resolve()
    try:
        target_dir.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Export path escaped root: {target_dir}") from exc
    if layout == "skill_dir":
        return target_dir / slug / "SKILL.md"
    return target_dir / f"{slug}.md"


def _safe_write(path: Path, content: str) -> None:
    import errno
    import os
    resolved = path.resolve()
    if path.exists() and path.is_symlink():
        raise OSError(f"Refusing to overwrite symlink: {path}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(resolved), flags, 0o666)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError(f"Refusing to write through symlink: {path}") from exc
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------

def _build_content(agent_row: dict, provider_id: str) -> str:
    """Build provider-native skill/agent markdown from agent_definitions row."""
    definition = agent_row.get("definition") or ""
    if provider_id == "claude-code":
        return definition.strip() + "\n"
    # Other providers: strip any claude-specific frontmatter keys and return body
    # (Copilot/OpenCode accept simpler markdown)
    lines = definition.strip().splitlines()
    out: list[str] = []
    in_frontmatter = False
    fm_done = False
    fm_lines: list[str] = []
    for i, line in enumerate(lines):
        if i == 0 and line.strip() == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line.strip() == "---":
                in_frontmatter = False
                fm_done = True
                # Rewrite frontmatter — keep name/description, drop claude-specific keys
                out.append("---")
                for fl in fm_lines:
                    if fl.startswith(("name:", "description:")):
                        out.append(fl)
                out.append("---")
            else:
                fm_lines.append(line)
        else:
            out.append(line)
    if not fm_done:
        out = lines
    return "\n".join(out).strip() + "\n"


# ---------------------------------------------------------------------------
# Core export
# ---------------------------------------------------------------------------

def export_agent_skill(
    db: Database,
    agent_id: str,
    *,
    providers: list[str] | None = None,
    scope: str = "project",          # "project" or "user"
    project_path: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Export one approved agent definition as skill files.

    Returns {"written": [...], "skipped": [...], "errors": [...]}.
    Raises ValueError for non-active agents (surface API convention).
    """
    agent_row = db.get_agent_definition(agent_id) or db.agent_definition_get(agent_id)
    if not agent_row:
        raise ValueError(f"Agent not found: {agent_id}")

    state = (agent_row.get("promotion_state") or "").lower()
    if state != "active":
        raise ValueError(
            f"Agent {agent_id!r} has promotion_state={state!r}; only 'active' agents may be exported"
        )

    slug = _slugify(agent_row.get("pattern_desc") or agent_row.get("description") or agent_id)
    provider_ids = providers or ["claude-code"]
    written: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    for pid in provider_ids:
        target = _TARGET_BY_PROVIDER.get(pid)
        if target is None:
            skipped.append({"provider": pid, "reason": "unknown provider"})
            continue

        try:
            content = _build_content(agent_row, pid)

            if scope == "user":
                if target.layout == "skill_dir":
                    out_path = target.global_dir.joinpath(slug, "SKILL.md")
                else:
                    out_path = target.global_dir.joinpath(f"{slug}.md")
            else:
                # project scope
                if not project_path:
                    skipped.append({"provider": pid, "reason": "project_path required for scope=project"})
                    continue
                root = Path(project_path).expanduser().resolve(strict=False)
                if not root.is_dir():
                    errors.append({"provider": pid, "reason": f"project_path not a directory: {root}"})
                    continue
                out_path = _resolve_export_path(root, target.project_subdir, slug, target.layout)

            if not dry_run:
                _safe_write(out_path, content)
            written.append({"provider": pid, "path": str(out_path), "dry_run": dry_run})

            db.agent_audit_log(
                agent_id=agent_id,
                event_type="skill_exported" if scope == "project" else "skill_promoted_global",
                target=pid,
                details={"path": str(out_path), "scope": scope, "dry_run": dry_run},
            )
        except Exception as exc:
            log.debug("export failed for provider %s: %s", pid, exc, exc_info=True)
            errors.append({"provider": pid, "reason": str(exc)})

    return {"written": written, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Review dimension definitions (prompt economy)
# ---------------------------------------------------------------------------

# Read-only: review agents report findings, they never edit. Emitted only for
# providers in TOOL_RESTRICTION_CAPABLE_SHELLS, whose frontmatter format is known.
_REVIEW_TOOLS = "Read, Grep, Glob"

# A named `subagent_type` resolves from the host's *agent* registry, which is not
# always the same place learned skills go. Claude Code is the case that differs:
# `_BUILTIN_TARGETS` points at `.claude/skills` for learned agents, but a spawn's
# subagent_type is looked up in `.claude/agents`.
_REVIEW_TARGET_OVERRIDES: dict[str, ExportTarget] = {
    "claude-code": ExportTarget(
        provider_id="claude-code",
        project_subdir=".claude/agents",
        global_dir=Path.home() / ".claude" / "agents",
        layout="flat_md",
    ),
}

# Filenames that already define a reviewer under a given name. Users commonly ship
# their own tuned reviewers (`review-security.agent.md`); those are exactly the
# definitions this feature wants the host to load, so they must never be overwritten.
_REVIEW_DEFINITION_SUFFIXES = (".md", ".agent.md")


def _review_definition_exists(directory: Path, slug: str, layout: str) -> Path | None:
    """Return an existing definition path for *slug*, or None."""
    if layout == "skill_dir":
        candidate = directory / slug / "SKILL.md"
        return candidate if candidate.exists() else None
    for suffix in _REVIEW_DEFINITION_SUFFIXES:
        candidate = directory / f"{slug}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _build_review_definition(dim: "Any", provider_id: str) -> str:
    """Render one review dimension as a provider-native definition file.

    The body is ``dim.stable_block`` — the same instruction text the inline prompt
    would carry. Exporting it here is what lets the per-agent prompt shrink to just
    the target path: with N cells the text is loaded once per agent by the host
    instead of being paid for N times in prompt tokens.
    """
    from .config import TOOL_RESTRICTION_CAPABLE_SHELLS

    description = (
        f"{dim.title} agent for Threnody review fanout. Read-only: reports findings, "
        "never edits."
    )
    lines = ["---", f"name: {dim.subagent_type}", f"description: {description}"]
    if provider_id in TOOL_RESTRICTION_CAPABLE_SHELLS:
        lines.append(f"tools: {_REVIEW_TOOLS}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {dim.title}")
    lines.append("")
    lines.append(dim.stable_block)
    lines.append("")
    lines.append(
        "The spawn prompt names the file to review and may add static pre-scan leads "
        "to confirm or refute. Review only the named file."
    )
    return "\n".join(lines) + "\n"


def export_review_definitions(
    *,
    providers: list[str] | None = None,
    scope: str = "user",
    project_path: str | None = None,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict:
    """Write the review-dimension definitions into each host's native directory.

    Unlike :func:`export_agent_skill` these are not learned agents — there is no DB
    row and no approval gate, because the content is Threnody's own static review
    instructions taken straight from ``review_fanout.REVIEW_DIMENSIONS``. One source
    of truth: the exported file and the inline fallback prompt render from the same
    dimension record, so they cannot drift.

    An existing definition of the same name is left alone unless *overwrite* is set:
    a user's own tuned reviewer is precisely the definition this is trying to put in
    place, so clobbering it would destroy the better version of the same thing.

    Returns ``{"written": [...], "skipped": [...], "errors": [...]}``.
    """
    from .review_fanout import REVIEW_DIMENSIONS

    provider_ids = providers or [t.provider_id for t in _BUILTIN_TARGETS]
    written: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    for pid in provider_ids:
        target = _REVIEW_TARGET_OVERRIDES.get(pid) or _TARGET_BY_PROVIDER.get(pid)
        if target is None:
            skipped.append({"provider": pid, "reason": "unknown provider"})
            continue
        for dim in REVIEW_DIMENSIONS:
            slug = dim.subagent_type
            try:
                content = _build_review_definition(dim, pid)
                if scope == "user":
                    if target.layout == "skill_dir":
                        out_path = target.global_dir.joinpath(slug, "SKILL.md")
                    else:
                        out_path = target.global_dir.joinpath(f"{slug}.md")
                    existing = _review_definition_exists(
                        target.global_dir, slug, target.layout
                    )
                    if existing is not None and not overwrite:
                        skipped.append(
                            {
                                "provider": pid,
                                "dimension": dim.key,
                                "reason": "definition already present",
                                "path": str(existing),
                            }
                        )
                        continue
                else:
                    if not project_path:
                        skipped.append(
                            {
                                "provider": pid,
                                "reason": "project_path required for scope=project",
                            }
                        )
                        break
                    root = Path(project_path).expanduser().resolve(strict=False)
                    if not root.is_dir():
                        errors.append(
                            {"provider": pid, "reason": f"project_path not a directory: {root}"}
                        )
                        break
                    out_path = _resolve_export_path(
                        root, target.project_subdir, slug, target.layout
                    )
                    existing = _review_definition_exists(
                        out_path.parent if target.layout == "flat_md" else root / target.project_subdir,
                        slug,
                        target.layout,
                    )
                    if existing is not None and not overwrite:
                        skipped.append(
                            {
                                "provider": pid,
                                "dimension": dim.key,
                                "reason": "definition already present",
                                "path": str(existing),
                            }
                        )
                        continue
                if not dry_run:
                    _safe_write(out_path, content)
                written.append(
                    {
                        "provider": pid,
                        "dimension": dim.key,
                        "path": str(out_path),
                        "dry_run": dry_run,
                    }
                )
            except Exception as exc:
                log.debug(
                    "review definition export failed for %s/%s: %s",
                    pid,
                    dim.key,
                    exc,
                    exc_info=True,
                )
                errors.append({"provider": pid, "dimension": dim.key, "reason": str(exc)})

    return {"written": written, "skipped": skipped, "errors": errors}


def export_all_active(
    db: Database,
    *,
    providers: list[str] | None = None,
    scope: str = "project",
    project_path: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    active = db.get_active_agents() or []
    results = []
    for agent in active:
        agent_id = agent.get("pattern_hash") or agent.get("id") or ""
        if not agent_id:
            continue
        try:
            r = export_agent_skill(
                db, agent_id,
                providers=providers, scope=scope,
                project_path=project_path, dry_run=dry_run,
            )
            r["agent_id"] = agent_id
            results.append(r)
        except Exception as exc:
            log.debug("export_all_active: skipping %s: %s", agent_id, exc, exc_info=True)
            results.append({"agent_id": agent_id, "written": [], "skipped": [], "errors": [str(exc)]})
    return results


# ---------------------------------------------------------------------------
# Promotion helper (called from warm path — best-effort)
# ---------------------------------------------------------------------------

def check_and_promote(
    db: Database,
    agent_id: str,
    config: TGsConfig,
) -> dict:
    """Promote a project-scoped skill to user-global when match_count reaches threshold.

    Best-effort: caller must wrap in try-except and log on failure.
    """
    threshold = getattr(config, "skill_promotion_threshold", 10)
    auto = getattr(config, "skill_auto_promote", False)
    providers = getattr(config, "skill_export_providers", ["claude-code"])

    if not auto:
        return {"promoted": False, "reason": "auto_promote disabled"}

    row = db.get_agent_definition(agent_id) or db.agent_definition_get(agent_id)
    if not row:
        return {"promoted": False, "reason": "not found"}

    if (row.get("promotion_state") or "") != "active":
        return {"promoted": False, "reason": "not active"}

    match_count = row.get("match_count") or 0
    if match_count < threshold:
        return {"promoted": False, "reason": f"match_count={match_count} < threshold={threshold}"}

    if row.get("exported_global_ts"):
        return {"promoted": False, "reason": "already promoted"}

    result = export_agent_skill(db, agent_id, providers=providers, scope="user")

    if result.get("written"):
        import time
        with db.conn() as conn:
            conn.execute(
                "UPDATE agent_definitions SET exported_global_ts = ? WHERE pattern_hash = ?",
                (time.time(), agent_id),
            )
        return {"promoted": True, "written": result.get("written", [])}

    return {"promoted": False, "reason": "export produced no written files", "detail": result}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export learned agents as provider skill files")
    parser.add_argument("agent_id", nargs="?", help="Pattern hash or ID of agent to export")
    parser.add_argument("--all-active", action="store_true", help="Export all active agents")
    parser.add_argument("--provider", action="append", dest="providers", help="Provider ID (repeatable)")
    parser.add_argument("--global", dest="global_scope", action="store_true", help="Export to user-global skills dir")
    parser.add_argument("--project", default=None, help="Project root path (default: cwd)")
    parser.add_argument("--check-promotions", action="store_true", help="Check and promote agents at threshold")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args(argv)

    db = open_database(args.db)
    try:
        scope = "user" if args.global_scope else "project"
        project_path = args.project or str(Path.cwd())

        if args.check_promotions:
            cfg = TGsConfig.from_yaml()
            active = db.get_active_agents() or []
            promoted = 0
            for agent in active:
                aid = agent.get("pattern_hash") or agent.get("id") or ""
                if not aid:
                    continue
                try:
                    r = check_and_promote(db, aid, cfg)
                    if r.get("promoted"):
                        promoted += 1
                        print(f"promoted: {aid}")
                except Exception as exc:
                    log.debug("promotion check failed for %s: %s", aid, exc, exc_info=True)
            print(f"total promoted: {promoted}")
            return 0

        if args.all_active:
            results = export_all_active(
                db, providers=args.providers, scope=scope,
                project_path=project_path, dry_run=args.dry_run,
            )
            for r in results:
                _print_result(r)
            return 0

        if not args.agent_id:
            parser.error("Provide an agent_id or --all-active")

        result = export_agent_skill(
            db, args.agent_id,
            providers=args.providers, scope=scope,
            project_path=project_path, dry_run=args.dry_run,
        )
        _print_result(result)
        return 0 if not result.get("errors") else 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        log.error("export failed: %s", exc, exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


def _print_result(result: dict) -> None:
    agent_id = result.get("agent_id", "")
    prefix = f"[{agent_id}] " if agent_id else ""
    for w in result.get("written", []):
        tag = "(dry-run) " if w.get("dry_run") else ""
        print(f"{prefix}written {tag}{w.get('provider')}: {w.get('path')}")
    for s in result.get("skipped", []):
        print(f"{prefix}skipped {s.get('provider')}: {s.get('reason')}")
    for e in result.get("errors", []):
        print(f"{prefix}ERROR {e.get('provider', '?')}: {e.get('reason')}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
