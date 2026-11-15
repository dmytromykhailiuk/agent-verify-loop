"""Verification roles: the review roles (generalist reviewer + specialists) and QA."""
from __future__ import annotations

REVIEW_ROLES = ("reviewer", "security", "perf", "ux")
SPECIALISTS = ("security", "perf", "ux")
ALL_ROLES = (*REVIEW_ROLES, "qa")
ADVISORY_ROLES = ("perf", "ux")
PREFIX = {"reviewer": "R", "security": "SC", "perf": "PF", "ux": "UX", "qa": "Q"}
SUGGESTION_PREFIX = {"reviewer": "S", "security": "SCS", "perf": "PFS", "ux": "UXS", "qa": "QS"}
AGENT = {"reviewer": "verify-reviewer", "security": "verify-security", "perf": "verify-perf", "ux": "verify-ux",
         "qa": "verify-qa"}
UI_SPEC = ("clear", "partial", "none", "n/a")
SPECIALIST_SETTINGS = ("auto", "on", "off")


def is_review_role(role: str | None) -> bool:
    return role in REVIEW_ROLES
