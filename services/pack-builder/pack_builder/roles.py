"""Authoring roles and permissions (AUT-01): TABLE AUT-01a and TABLE AUT-01b, enforced in code.

The CLI, CI and the signing service call `require` before acting; a refusal raises `Forbidden` and is written to the
audit log by the caller. Roles exist only in the authoring system, never on a student device. There is no reviewer
role and no approval action: content is published after the automated gates (D65).
"""

from __future__ import annotations

from collections.abc import Iterable

ROLES = ("curriculum_lead", "subject_lead", "subject_author", "language_reviewer")
S1_ROLES = ROLES[:3]  # language_reviewer arrives with translations (S4, AUT-40); there is no reviewer role (D65)

# Action -> roles allowed (TABLE AUT-01b as amended by D65: no approval actions). A role absent from an action's set
# is a "—" cell. Qualified cells are separate actions: a subject author edits the map by proposal only; a language
# reviewer acts on translations only.
PERMISSIONS: dict[str, frozenset[str]] = {
    "edit_draft": frozenset({"curriculum_lead", "subject_lead", "subject_author"}),
    "edit_translation": frozenset({"curriculum_lead", "subject_lead", "subject_author", "language_reviewer"}),
    "ai_draft": frozenset({"curriculum_lead", "subject_lead", "subject_author"}),
    "ai_draft_translation": frozenset({"curriculum_lead", "subject_lead", "subject_author", "language_reviewer"}),
    "bulk_import": frozenset({"curriculum_lead", "subject_lead", "subject_author"}),
    "request_publication": frozenset({"curriculum_lead"}),
    "retire_or_migrate": frozenset({"curriculum_lead", "subject_lead"}),
    "edit_map": frozenset({"curriculum_lead", "subject_lead"}),
    "propose_map_change": frozenset({"curriculum_lead", "subject_lead", "subject_author"}),
    "edit_licence_register": frozenset({"curriculum_lead"}),
    "see_analytics": frozenset({"curriculum_lead", "subject_lead", "subject_author"}),
}

SECOND_FACTOR: frozenset[str] = frozenset()


class Forbidden(PermissionError):
    """An action a role may never take (a "—" cell)."""

    code = "forbidden"


def allowed(roles: Iterable[str], action: str) -> bool:
    return bool(set(roles) & PERMISSIONS[action])


def require(roles: Iterable[str], action: str) -> None:
    """Refuse an action the roles may not take."""
    roles = set(roles)
    if action not in PERMISSIONS:
        raise ValueError(f"unknown action {action!r}")
    if not allowed(roles, action):
        raise Forbidden(f"{action}: not allowed for {', '.join(sorted(roles)) or 'no role'} (AUT-01)")
