"""Shared SupportCommand protocol between semantic interpretation and Runtime."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from agent.goal_taxonomy import canonicalize_goal, is_canonical_goal

# Keep the semantic protocol small.  This mirrors mature command-generator
# designs: start a business flow, fill/correct a slot, cancel/skip/interrupt.
# Runtime state decides whether a subject assignment is an initial fill,
# a correction, or an answer to a pending choice.
COMMAND_TYPES = {
    "start_goal",
    "set_subject",
    "reject_pending",
    "interrupt",
    "cancel_goal",
}

# Migration scope is an explicit architecture decision, not a parser tweak.
SUPPORTED_GOALS = {
    ("order", "list"),
    ("order", "status"),
    ("refund", "request"),
    ("refund", "status"),
}

SCOPE_VALUES = {"supported", "other", "uncertain"}
SAFE_CANDIDATE_REF_RE = re.compile(r"^(?:choice_[1-9][0-9]{0,2}|current_subject)$")


@dataclass(frozen=True)
class SupportCommand:
    """One model-proposed dialogue command; never executable authority by itself."""

    type: str
    domain: str = ""
    operation: str = ""
    subject_description: str = ""
    candidate_ref: str = ""
    confidence: float = 0.0

    @property
    def goal(self) -> str:
        return f"{self.domain}.{self.operation}" if self.domain and self.operation else ""


@dataclass(frozen=True)
class SupportCommandTurn:
    """Bounded semantic interpretation of one user turn."""

    scope: str
    commands: tuple[SupportCommand, ...]


def _bounded_text(value: object, *, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


def _bounded_confidence(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def parse_support_command_turn(value: object) -> SupportCommandTurn:
    """Validate model JSON without promoting it to business authority."""

    payload = value
    if isinstance(payload, str):
        payload = payload.strip()
        if payload.startswith("```"):
            lines = payload.splitlines()
            payload = "\n".join(lines[1:-1]) if len(lines) >= 3 else payload
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError("support command output must be a JSON object")

    scope = _bounded_text(payload.get("scope"), max_length=20).lower()
    if scope not in SCOPE_VALUES:
        scope = "uncertain"

    raw_commands = payload.get("commands")
    if not isinstance(raw_commands, list):
        raw_commands = []

    commands: list[SupportCommand] = []
    for raw in raw_commands[:3]:
        if not isinstance(raw, dict):
            continue
        command_type = _bounded_text(raw.get("type"), max_length=40).lower()
        if command_type not in COMMAND_TYPES:
            continue

        domain = _bounded_text(raw.get("domain"), max_length=40).lower()
        operation = _bounded_text(raw.get("operation"), max_length=60).lower()
        # Only flow-start commands declare a Goal.  Subject assignment and
        # repair commands apply to Runtime state instead of carrying a second
        # potentially conflicting flow identity.
        if command_type != "start_goal":
            domain = ""
            operation = ""
        elif domain or operation:
            domain, operation = canonicalize_goal(domain, operation)
            if not is_canonical_goal(domain, operation) or (domain, operation) not in SUPPORTED_GOALS:
                domain = ""
                operation = ""

        candidate_ref = _bounded_text(raw.get("candidate_ref"), max_length=32)
        if candidate_ref and not SAFE_CANDIDATE_REF_RE.fullmatch(candidate_ref):
            candidate_ref = ""

        commands.append(
            SupportCommand(
                type=command_type,
                domain=domain,
                operation=operation,
                subject_description=_bounded_text(raw.get("subject_description"), max_length=240),
                candidate_ref=candidate_ref,
                confidence=_bounded_confidence(raw.get("confidence")),
            )
        )

    return SupportCommandTurn(scope=scope, commands=tuple(commands))
