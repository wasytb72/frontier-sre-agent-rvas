import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "break-sample-food-app.sh"
TAG = "rvas-food-original-target-port"

MOCK = r"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path(os.environ["MOCK_STATE"])
state = json.loads(state_path.read_text())

def value(flag):
    return args[args.index(flag) + 1]

if tool == "terraform":
    print(json.dumps({
        "rvas_resource_group_name": {"value": "lab-rg"},
        "sample_food_api_container_app_name": {"value": "lab-api"},
    }))
elif tool == "az":
    state["calls"].append(args)
    if args[:2] == ["containerapp", "show"]:
        print(json.dumps({
            "id": "/subscriptions/test/resourceGroups/lab-rg/providers/Microsoft.App/containerApps/lab-api",
            "tags": state["tags"],
            "properties": {"configuration": {"ingress": {
                "external": True, "transport": "auto",
                "targetPort": state["port"], "fqdn": "lab.invalid",
            }}},
        }))
    elif args[:3] == ["containerapp", "ingress", "update"]:
        if state.get("update_failure"):
            state_path.write_text(json.dumps(state))
            print("Simulated Azure update failure", file=sys.stderr)
            sys.exit(1)
        state["port"] = int(value("--target-port"))
    elif args[:2] == ["tag", "update"]:
        key, tag_value = value("--tags").split("=", 1)
        if state.get("tag_failure"):
            state_path.write_text(json.dumps(state))
            sys.exit(1)
        if value("--operation") == "Merge":
            state["tags"][key] = tag_value
        else:
            del state["tags"][key]
    else:
        raise RuntimeError(args)
elif tool == "curl":
    state["probes"] += 1
    if state.get("transport_failure"):
        print("000", end="")
        state_path.write_text(json.dumps(state))
        sys.exit(7)
    if state["port"] == 1:
        code = state.get("fault_code", "503")
        if state.get("fault_codes"):
            code = state["fault_codes"].pop(0)
        if state.get("one_healthy_route") and args[-1].endswith("/api/FoodItems"):
            code = "200"
    else:
        code = state.get("healthy_code", "200")
    print(code, end="")
elif tool != "sleep":
    raise RuntimeError(tool)
state_path.write_text(json.dumps(state))
"""


class FoodFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.state_path.write_text(json.dumps({
            "port": 8080, "tags": {"owner": "lab"}, "calls": [], "probes": 0,
        }))
        for tool in ("az", "terraform", "curl", "sleep"):
            executable = self.root / tool
            executable.write_text(MOCK)
            executable.chmod(0o700)
        self.env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}",
                        MOCK_STATE=str(self.state_path))

    def state(self):
        return json.loads(self.state_path.read_text())

    def update(self, **values):
        state = self.state()
        state.update(values)
        self.state_path.write_text(json.dumps(state))

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_break_and_restore_round_trip(self):
        result = self.run_script("break", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()["port"], 1)
        self.assertEqual(self.state()["tags"][TAG], "8080")
        self.assertIn("Verified ingress outage", result.stdout)
        self.assertEqual(self.state()["probes"], 12)
        result = self.run_script("restore", "--yes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()["port"], 8080)
        self.assertEqual(self.state()["tags"], {"owner": "lab"})
        self.assertEqual(self.state()["probes"], 18)

    def test_repeated_break_preserves_original_port(self):
        self.assertEqual(self.run_script("break", "--yes").returncode, 0)
        self.assertNotEqual(self.run_script("break", "--yes").returncode, 0)
        self.assertEqual(self.state()["tags"][TAG], "8080")

    def test_healthy_api_is_not_reported_as_broken(self):
        self.update(fault_code="200")
        result = self.run_script("break", "--yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Verified ingress outage", result.stdout)
        self.assertEqual(self.state()["tags"][TAG], "8080")
        self.assertEqual(self.state()["probes"], 54)

    def test_both_routes_must_fail(self):
        self.update(one_healthy_route=True)
        self.assertNotEqual(self.run_script("break", "--yes").returncode, 0)

    def test_success_requires_three_consecutive_matching_probes(self):
        self.update(fault_codes=["503", "503", "503", "503", "200", "503"])
        self.assertEqual(self.run_script("break", "--yes").returncode, 0)
        self.assertEqual(self.state()["probes"], 18)

    def test_restore_uses_saved_port_not_a_hardcoded_default(self):
        self.update(port=9090)
        self.assertEqual(self.run_script("break", "--yes").returncode, 0)
        self.assertEqual(self.state()["tags"][TAG], "9090")
        self.assertEqual(self.run_script("restore", "--yes").returncode, 0)
        self.assertEqual(self.state()["port"], 9090)

    def test_connection_failure_is_not_an_outage_success(self):
        self.update(port=1, tags={TAG: "8080"}, transport_failure=True)
        result = self.run_script("restore", "--yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.state()["tags"][TAG], "8080")
        self.assertIn("transport failure is not outage evidence", result.stderr)
        self.assertNotIn("000000", result.stdout)

    def test_unhealthy_baseline_does_not_mutate_azure(self):
        self.update(healthy_code="503")
        self.assertNotEqual(self.run_script("break", "--yes").returncode, 0)
        self.assertEqual(self.state()["port"], 8080)
        self.assertEqual(self.state()["tags"], {"owner": "lab"})
        self.assertEqual(len(self.state()["calls"]), 1)

    def test_failed_restore_keeps_recovery_tag(self):
        self.update(port=1, tags={TAG: "8080"}, healthy_code="503")
        self.assertNotEqual(self.run_script("restore", "--yes").returncode, 0)
        self.assertEqual(self.state()["tags"][TAG], "8080")

    def test_missing_or_invalid_saved_port_is_rejected(self):
        for saved in ("", "1", "0", "65536", "bad", "08080"):
            with self.subTest(saved=saved):
                self.update(tags={TAG: saved})
                self.assertNotEqual(self.run_script("restore", "--yes").returncode, 0)
                self.assertEqual(self.state()["port"], 8080)

    def test_external_port_change_is_not_overwritten(self):
        self.update(port=9090, tags={TAG: "8080"})
        self.assertNotEqual(self.run_script("restore", "--yes").returncode, 0)
        self.assertEqual(self.state()["port"], 9090)

    def test_save_failure_does_not_change_port(self):
        self.update(tag_failure=True)
        self.assertNotEqual(self.run_script("break", "--yes").returncode, 0)
        self.assertEqual(self.state()["port"], 8080)

    def test_update_failure_retains_saved_state(self):
        self.update(update_failure=True)
        self.assertNotEqual(self.run_script("break", "--yes").returncode, 0)
        self.assertEqual(self.state()["tags"][TAG], "8080")
        self.update(update_failure=False)
        self.assertEqual(self.run_script("restore", "--yes").returncode, 0)

    def test_explicit_action_and_confirmation_required(self):
        for args in ((), ("break",), ("restore",), ("https://lab.invalid", "200"),
                     ("break", "--yes", "extra")):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_script(*args).returncode, 0)
        self.assertEqual(self.state()["calls"], [])

    def test_help_requires_no_azure_access(self):
        self.assertEqual(self.run_script("--help").returncode, 0)
        self.assertEqual(self.state()["calls"], [])


if __name__ == "__main__":
    unittest.main()
