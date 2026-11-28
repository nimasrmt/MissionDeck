"""Version 3: explicit user-owned connections, including input and delivery."""
from __future__ import annotations

from copy import deepcopy
from app.plan import PlanValidationError


def compile_v3(workflow, mid, objective, eligible):
    from app.team_workflow_v2 import compile_v2

    source = deepcopy(workflow)
    blocks = source.get("blocks")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 8 or not all(isinstance(b, dict) for b in blocks):
        raise PlanValidationError(["Choose 1..8 team blocks"])
    ids = [b.get("id") for b in blocks]
    if not all(isinstance(i, str) and i not in ("__input", "__output") for i in ids) or len(set(ids)) != len(ids):
        raise PlanValidationError(["Every block needs a unique ID"])
    by_id = {b["id"]: b for b in blocks}
    links = source.get("connections")
    if not isinstance(links, list) or len(links) > 80:
        raise PlanValidationError(["Connections must be a list of at most 80 explicit links"])
    errors, edges = [], set()
    for link in links:
        if not isinstance(link, dict) or set(link) != {"source", "target"}:
            errors.append("Each connection must contain source and target IDs"); continue
        a, b = link["source"], link["target"]
        if not isinstance(a, str) or not isinstance(b, str) or a not in {"__input", *ids} or b not in {"__output", *ids} or a == b:
            errors.append("Connection endpoints are invalid"); continue
        if a == "__input" and b == "__output":
            errors.append("Connect input through at least one team before final delivery"); continue
        if a in by_id and by_id[a].get("kind") == "inspection" and b != "__output":
            errors.append("The final inspector connects to Final delivery"); continue
        if (a, b) in edges: errors.append("Duplicate connection")
        edges.add((a, b))
    if errors: raise PlanValidationError(errors)

    def walk(start, reverse=False):
        seen, todo = set(), [start]
        while todo:
            node = todo.pop()
            if node in seen: continue
            seen.add(node)
            todo.extend(a if reverse else b for a, b in edges if (b if reverse else a) == node)
        return seen

    reached, delivered = walk("__input"), walk("__output", True)
    for bid in ids:
        if bid not in reached: errors.append(f"{by_id[bid].get('name', bid)}: connect a path from Mission input")
        if bid not in delivered: errors.append(f"{by_id[bid].get('name', bid)}: connect a path to Final delivery")
    if "__output" not in reached: errors.append("Connect Mission input to Final delivery through your teams")
    if errors: raise PlanValidationError(errors)

    for b in blocks:
        b["deps"] = sorted(a for a, dest in edges if dest == b["id"] and a != "__input")
    inlet = source.get("input")
    if inlet is None: inlet = {"text": objective, "files": []}
    if not isinstance(inlet, dict): raise PlanValidationError(["Input must be an object"])
    inlet["targets"] = sorted(b for a, b in edges if a == "__input")
    source["input"] = inlet
    connected = [a for a, b in sorted(edges) if b == "__output"]
    inspectors = [a for a in connected if by_id[a].get("kind") == "inspection"]
    sources = [a for a in connected if by_id[a].get("kind") != "inspection"]
    if inspectors:
        # Delivery through a review carries the reviewed producer files. It does
        # not add producer -> output edges to the user's graph.
        for a, b in sorted(edges):
            if b == inspectors[0] and a in by_id and by_id[a].get("kind") != "inspection" and a not in sources:
                sources.append(a)
    delivery = source.get("delivery")
    if delivery is not None and not isinstance(delivery, dict):
        raise PlanValidationError(["Delivery must be an object"])
    source["delivery"] = {**(delivery or {}), "sources": sources, "inspector": inspectors[0] if inspectors else None}
    source["version"] = 2
    source["wiring"] = "manual"
    plan = compile_v2(source, mid, objective, eligible)
    plan["team_workflow"]["version"] = 3
    plan["team_workflow"]["connections"] = deepcopy(links)
    return plan
