"""Explicit endpoint wiring must not infer or run disconnected work."""
import copy
import unittest
from app.team_workflow import compile_team_workflow
from app.data_flow import project_flow
from app.plan import PlanValidationError
from tests.test_manager_council import flow


def manual_flow():
    w = flow()
    w.update(version=3, connections=[{"source": a, "target": b} for a, b in [
        ("__input", "MGR"), ("MGR", "ENG"), ("ENG", "QA"), ("QA", "__output")]])
    return w


class ManualWiringTests(unittest.TestCase):
    def test_only_drawn_connections_are_saved_and_projected(self):
        w = manual_flow()
        plan = compile_team_workflow(w, "mis_wires", "Goal", {"cursor"})
        self.assertEqual(plan["team_workflow"]["connections"], w["connections"])
        self.assertEqual(plan["team_workflow"]["version"], 3)
        self.assertEqual(plan["team_workflow"]["delivery"]["sources"], ["ENG"])
        result = project_flow({"plan": plan, "tasks": [], "artifacts": [], "status": "awaiting_approval"})
        self.assertEqual({(e["source"], e["target"]) for e in result["edges"]}, {(e["source"], e["target"]) for e in w["connections"]})
        final = next(e for e in result["edges"] if e["target"] == "__output")
        self.assertTrue(any(f["path"] == "out/report.md" for f in final["files"]))

    def test_no_links_or_detached_blocks_cannot_be_approved(self):
        for index in range(4):
            w = manual_flow(); w["connections"].pop(index)
            with self.subTest(index=index), self.assertRaisesRegex(PlanValidationError, "connect"):
                compile_team_workflow(w, "mis_wires", "Goal", {"cursor"})
        w = manual_flow(); w["connections"] = []
        with self.assertRaises(PlanValidationError): compile_team_workflow(w, "mis_wires", "Goal", {"cursor"})

    def test_dependencies_are_derived_from_links_never_old_metadata(self):
        w = manual_flow()
        for b in w["blocks"]: b["deps"] = ["invented"]
        w["delivery"] = {"sources": ["invented"], "inspector": "invented"}
        plan = compile_team_workflow(w, "mis_wires", "Goal", {"cursor"})
        by_id = {b["id"]: b for b in plan["team_workflow"]["blocks"]}
        self.assertEqual(by_id["ENG"]["deps"], ["MGR"])
        self.assertEqual(by_id["MGR"]["deps"], [])

    def test_rejects_duplicate_reverse_unknown_and_cyclic_links(self):
        for edge in [("MGR", "ENG"), ("ENG", "__input"), ("__output", "ENG"), ("missing", "ENG"), ("ENG", "MGR"), ("QA", "ENG"), ("__input", "__output")]:
            w = manual_flow(); w["connections"].append(dict(zip(("source", "target"), edge)))
            with self.subTest(edge=edge), self.assertRaises(PlanValidationError): compile_team_workflow(w, "mis_wires", "Goal", {"cursor"})

    def test_input_files_follow_the_explicit_input_connections(self):
        w = manual_flow(); w["input"]["files"] = ["source/data.txt"]
        w["connections"].append({"source": "__input", "target": "ENG"})
        plan = compile_team_workflow(w, "mis_wires", "Goal", {"cursor"})
        tasks = {t["id"]: t for t in plan["tasks"]}
        self.assertIn("source/data.txt", tasks["ENG_brief"]["inputs"])
        self.assertNotIn("source/data.txt", tasks["QA_check"]["inputs"])
        self.assertIn(".orca-runs/mis_wires/teams/MGR/manager.md", tasks["ENG_brief"]["inputs"])

    def test_optional_inspector_may_be_removed_without_inventing_delivery_links(self):
        w = manual_flow(); w["blocks"].pop()
        w["connections"] = w["connections"][:2] + [{"source": "ENG", "target": "__output"}]
        plan = compile_team_workflow(w, "mis_wires", "Goal", {"cursor"})
        self.assertIsNone(plan["team_workflow"]["delivery"]["inspector"])
        self.assertEqual(len(project_flow({"plan": plan})["edges"]), 3)


if __name__ == "__main__": unittest.main()
