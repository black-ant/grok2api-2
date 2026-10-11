import unittest

from app.dataplane.reverse.protocol.tool_prompt import (
    build_tool_system_prompt,
    normalize_tool_parameters,
)


class ToolPromptTests(unittest.TestCase):
    def test_filters_non_object_root_union_branches(self):
        parameters = {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {"task": {"type": "string"}},
                    "required": ["task"],
                },
                {"type": "string"},
                {"type": "null"},
            ]
        }

        normalized = normalize_tool_parameters(parameters)

        self.assertEqual(len(normalized["anyOf"]), 1)
        self.assertEqual(normalized["anyOf"][0]["type"], "object")
        self.assertEqual(len(parameters["anyOf"]), 3)

    def test_filters_non_object_one_of_branches(self):
        normalized = normalize_tool_parameters({
            "oneOf": [{"type": "object"}, {"type": "null"}],
        })

        self.assertEqual(normalized["oneOf"], [{"type": "object"}])

    def test_preserves_union_without_object_branch(self):
        parameters = {
            "oneOf": [{"type": "string"}, {"type": "null"}],
        }

        self.assertIs(normalize_tool_parameters(parameters), parameters)

    def test_only_normalizes_root_union_in_prompt(self):
        parameters = {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [{"type": "string"}, {"type": "null"}]
                }
            },
        }

        prompt = build_tool_system_prompt(
            [{
                "type": "function",
                "function": {"name": "echo", "parameters": parameters},
            }]
        )

        self.assertIn('"anyOf": [{"type": "string"}, {"type": "null"}]', prompt)
        self.assertEqual(
            parameters["properties"]["value"]["anyOf"],
            [{"type": "string"}, {"type": "null"}],
        )


if __name__ == "__main__":
    unittest.main()
