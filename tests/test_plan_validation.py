"""Plan validation: cycles, deps, duplicates, traversal, write conflicts, review independence."""

from __future__ import annotations

import pytest

from app.plan import PlanValidationError, validate_plan
from tests.conftest import valid_plan


def test_rejects_cycle() -> None:
    plan = valid_plan(
        tasks=[
            {
                "id": "T1",
                "title": "A",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": ["T2"],
                "write_scope": ["a.py"],
                "inputs": [],
                "outputs": ["a.py"],
                "acceptance": ["ok"],
                "validation": ["python a.py"],
                "review_required": False,
            },
            {
                "id": "T2",
                "title": "B",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": ["T1"],
                "write_scope": ["b.py"],
                "inputs": [],
                "outputs": ["b.py"],
                "acceptance": ["ok"],
                "validation": ["python b.py"],
                "review_required": False,
            },
        ]
    )
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan, eligible_agents={"cursor"})
    assert any("cycle" in e for e in ei.value.errors)


def test_rejects_missing_deps() -> None:
    plan = valid_plan()
    plan["tasks"][0]["deps"] = ["T999"]
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan, eligible_agents={"cursor"})
    assert any("unknown" in e for e in ei.value.errors)


def test_rejects_duplicate_ids() -> None:
    plan = valid_plan()
    t = dict(plan["tasks"][0])
    t["id"] = "T1"
    t["write_scope"] = ["other.py"]
    t["outputs"] = ["other.py"]
    plan["tasks"].append(t)
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan, eligible_agents={"cursor"})
    assert any("duplicate" in e for e in ei.value.errors)


def test_rejects_path_traversal() -> None:
    plan = valid_plan()
    plan["tasks"][0]["write_scope"] = ["../secret.txt"]
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan, eligible_agents={"cursor"})
    assert any("traversal" in e.lower() or "not allowed" in e.lower() for e in ei.value.errors)


def test_rejects_absolute_path() -> None:
    plan = valid_plan()
    plan["tasks"][0]["outputs"] = ["C:/Windows/system32/x.dll"]
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan, eligible_agents={"cursor"})
    assert any("Absolute" in e or "absolute" in e.lower() for e in ei.value.errors)


def test_rejects_concurrent_write_conflict() -> None:
    plan = valid_plan(
        tasks=[
            {
                "id": "T1",
                "title": "A",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": [],
                "write_scope": ["shared.py"],
                "inputs": [],
                "outputs": ["shared.py"],
                "acceptance": ["ok"],
                "validation": ["python shared.py"],
                "review_required": False,
            },
            {
                "id": "T2",
                "title": "B",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": [],
                "write_scope": ["shared.py"],
                "inputs": [],
                "outputs": ["shared.py"],
                "acceptance": ["ok"],
                "validation": ["python shared.py"],
                "review_required": False,
            },
        ]
    )
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan, eligible_agents={"cursor"})
    assert any("concurrent write" in e for e in ei.value.errors)


def test_allows_ordered_shared_write() -> None:
    plan = valid_plan(
        tasks=[
            {
                "id": "T1",
                "title": "A",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": [],
                "write_scope": ["shared.py"],
                "inputs": [],
                "outputs": ["shared.py"],
                "acceptance": ["ok"],
                "validation": ["python shared.py"],
                "review_required": False,
            },
            {
                "id": "T2",
                "title": "B",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": ["T1"],
                "write_scope": ["shared.py"],
                "inputs": [],
                "outputs": ["shared.py"],
                "acceptance": ["ok"],
                "validation": ["python shared.py"],
                "review_required": False,
            },
        ]
    )
    validate_plan(plan, eligible_agents={"cursor"})


def test_review_independence() -> None:
    plan = valid_plan(
        tasks=[
            {
                "id": "T1",
                "title": "Impl",
                "department": "eng",
                "role": "implementation",
                "agent": "cursor",
                "model": "unknown",
                "deps": [],
                "write_scope": ["src/a.py"],
                "inputs": [],
                "outputs": ["src/a.py"],
                "acceptance": ["ok"],
                "validation": ["python src/a.py"],
                "review_required": True,
            },
            {
                "id": "T2",
                "title": "Review",
                "department": "eng",
                "role": "review",
                "agent": "cursor",
                "model": "unknown",
                "deps": ["T1"],
                "write_scope": ["review.json"],
                "inputs": ["src/a.py"],
                "outputs": ["review.json"],
                "acceptance": ["PASS"],
                "validation": ["python -m pytest tests/test_example.py -q"],
                "review_required": False,
            },
        ]
    )
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan, eligible_agents={"cursor", "grok"})
    assert any("differ" in e for e in ei.value.errors)


def test_wrong_schema_version() -> None:
    plan = valid_plan(schema_version=2)
    with pytest.raises(PlanValidationError):
        validate_plan(plan, eligible_agents={"cursor"})


def test_zero_tasks() -> None:
    plan = valid_plan(tasks=[])
    with pytest.raises(PlanValidationError) as ei:
        validate_plan(plan)
    assert any("at least one" in e for e in ei.value.errors)


def test_git_path_rejected() -> None:
    plan = valid_plan()
    plan["tasks"][0]["write_scope"] = [".git/config"]
    with pytest.raises(PlanValidationError):
        validate_plan(plan, eligible_agents={"cursor"})
