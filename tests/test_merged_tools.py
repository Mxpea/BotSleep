"""Upstream calculator metadata does not advertise an unbound runtime action."""
import unittest

from agent.tools import STRONGHOLD_TOOL_DEFINITION, tools_for_objective


class MergedToolCatalogTests(unittest.TestCase):
    def test_standalone_calculator_schema_is_preserved_outside_survival_catalogs(self):
        function = STRONGHOLD_TOOL_DEFINITION["function"]
        self.assertEqual(function["name"], "calculate_stronghold")
        self.assertEqual(function["parameters"]["required"], ["throws"])
        ray = function["parameters"]["properties"]["throws"]["items"]
        self.assertEqual(set(ray["required"]), {"x", "z", "bearing_deg", "sigma_deg"})
        for objective, expected_count in (("starter", 9), ("daily", 31)):
            with self.subTest(objective=objective):
                tools = tools_for_objective(objective)
                names = {tool["function"]["name"] for tool in tools}
                self.assertEqual(len(names), expected_count)
                self.assertNotIn("calculate_stronghold", names)
                self.assertNotIn("pillar_up", names)
                self.assertNotIn("chat_say", names)


if __name__ == "__main__":
    unittest.main()
