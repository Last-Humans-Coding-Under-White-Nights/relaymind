"""Reviewed installed remote profile, never caller-provided authority (RM43)."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteSourceProfile:
    profile_id: str
    mechanism: str
    repository: str
    ref: str
    root: str
    api_version: str
    minimum_poll_seconds: int
    rights_sha256: str
    qualification_digest: str


PUBLIC_SKILLS = RemoteSourceProfile(
    "github.relaymind-skills.v1",
    "github_git_skills.v1",
    "LHCWN/relaymind",
    "heads/master",
    "skills",
    "2022-11-28",
    300,
    "a0df745636ded7b39d42761f4e5000fb0718c04e98be6b004bf00efdcd79509a",
    "d3089469be1a914da591186f0b9f8019c5a740b5a01942e6f19d54de12fd681a",
)


def resolve_remote_profile(contract_id: str) -> RemoteSourceProfile:
    if contract_id != PUBLIC_SKILLS.profile_id:
        raise ValueError("unknown qualified remote profile")
    return PUBLIC_SKILLS


def validate_secret_ref(value: object) -> None:
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]{0,127}", value) is None
    ):
        raise ValueError("invalid remote credential reference")
