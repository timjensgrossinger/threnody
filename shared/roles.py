"""Role derivation from task descriptions.

Maps task text to a semantic role label using keyword matching.
Precedence: Debugger > Tester > Reviewer > Implementer > Architect > Migrator > Documenter > Worker.
"""
from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)

_ROLE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Debugger", re.compile(
        r"\b(?:fix(?:ing)?|debug(?:ging)?|diagnos(?:e|ing)|resolv(?:e|ing)|"
        r"bug(?:s)?|error(?:s)?|crash(?:ing)?|trace(?:back)?(?:s)?|exception(?:s)?|fault(?:s)?|defect(?:s)?)\b",
        re.IGNORECASE,
    )),
    ("Tester", re.compile(
        r"\b(?:test(?:s|ing)?|verify(?:ing)?|validat(?:e|ing)|coverage|fuzz(?:ing)?|"
        r"spec(?:ification)?(?:s)?|assert(?:ion)?(?:s)?|mock(?:ing)?|stub(?:s)?)\b",
        re.IGNORECASE,
    )),
    ("Reviewer", re.compile(
        r"\b(?:review(?:ing|ed)?|audit(?:ing)?|check(?:ing)?|analyze?|"
        r"inspect(?:ing)?|examin(?:e|ing)|scrutiniz(?:e|ing)|evaluat(?:e|ing)|assess(?:ing)?|"
        r"vet(?:ting)?|scan(?:ning)?|apprais(?:e|ing))\b",
        re.IGNORECASE,
    )),
    ("Implementer", re.compile(
        r"\b(?:creat(?:e|ing)|add(?:ing)?|implement(?:ing)?|build(?:ing)?|"
        r"write|writ(?:ing)?|new feature(?:s)?|scaffold(?:ing)?|generat(?:e|ing)|"
        r"develop(?:ing)?|construct(?:ing)?|author(?:ing)?|produc(?:e|ing))\b",
        re.IGNORECASE,
    )),
    ("Architect", re.compile(
        r"\b(?:design(?:ing)?|plan(?:ning)?|refactor(?:ing)? (?:structure|architectur|module|layout)|"
        r"architect(?:ing|ure)|blueprint(?:ing)?|restructur(?:e|ing)|reorganiz(?:e|ing)|"
        r"redesign(?:ing)?|system design|module layout|api design)\b",
        re.IGNORECASE,
    )),
    ("Migrator", re.compile(
        r"\b(?:migrat(?:e|ing)|upgrad(?:e|ing)|convert(?:ing)?|port(?:ing)?|"
        r"moderniz(?:e|ing)|renovat(?:e|ing)|translat(?:e|ing)|adapt(?:ing)?|"
        r"transition(?:ing)?|switch(?:ing)?|transition(?:s)?|switchover)\b",
        re.IGNORECASE,
    )),
    ("Documenter", re.compile(
        r"\b(?:document(?:ing|ation)?|readme|docs?|comment(?:ing|s)?|"
        r"explaining?|annotat(?:e|ing)|docstring(?:s)?|javadoc|changelog|"
        r"inline doc|api doc|user guide|tutorial)\b",
        re.IGNORECASE,
    )),
]

DEFAULT_ROLE = "Worker"


def derive_role_from_task(description: str) -> str:
    """Derive a semantic role from task description text.

    Precedence: Debugger > Tester > Reviewer > Implementer > Architect > Migrator > Documenter.
    Returns DEFAULT_ROLE ("Worker") if no pattern matches.
    """
    if not description or not description.strip():
        return DEFAULT_ROLE
    for role, pattern in _ROLE_PATTERNS:
        if pattern.search(description):
            return role
    return DEFAULT_ROLE
