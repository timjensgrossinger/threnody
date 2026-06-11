#!/usr/bin/env python3
"""Ensure Threnody project skills exist with valid frontmatter."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / ".cursor" / "skills"

EXPECTED_SKILLS = (
    "threnody-plan",
    "threnody-routing",
    "threnody-task",
    "threnody-swarm",
    "threnody-fullstack",
    "threnody-subtasks",
)

FRONTMATTER_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


def test_project_skills_exist() -> None:
    assert SKILLS_DIR.is_dir(), f"missing skills directory: {SKILLS_DIR}"
    for skill_name in EXPECTED_SKILLS:
        skill_file = SKILLS_DIR / skill_name / "SKILL.md"
        assert skill_file.is_file(), f"missing skill file: {skill_file}"


def test_project_skill_frontmatter_names_match_directories() -> None:
    for skill_name in EXPECTED_SKILLS:
        skill_file = SKILLS_DIR / skill_name / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8")
        assert text.startswith("---"), f"{skill_file} must start with YAML frontmatter"
        match = FRONTMATTER_NAME.search(text)
        assert match is not None, f"{skill_file} missing name: in frontmatter"
        assert match.group(1) == skill_name, (
            f"{skill_file} name {match.group(1)!r} != directory {skill_name!r}"
        )
