import tempfile
import unittest
import math
from pathlib import Path

from graph_model import (
    NODE_SPECS,
    GraphDocument,
    GraphError,
    graph_to_script,
    graph_to_script_with_line_map,
    load_graph,
    new_graph,
    save_graph,
    script_to_graph,
)
from macro_core import parse_script, script_nodes_to_text


class GraphModelTests(unittest.TestCase):
    def test_new_graph_is_valid_and_compiles(self) -> None:
        graph = new_graph()
        graph.validate()
        self.assertEqual(graph_to_script(graph), "WAIT 0\n")

    def test_every_executable_node_has_valid_defaults(self) -> None:
        for spec in NODE_SPECS:
            if spec.type_name in {"start", "number_value", "text_value"}:
                continue
            with self.subTest(node=spec.type_name):
                graph = GraphDocument()
                start = graph.add_node("start")
                node = graph.add_node(spec.type_name)
                graph.add_link(start.id, "out", node.id, "in")
                if spec.type_name == "repeat":
                    body = graph.add_node("wait")
                    graph.add_link(node.id, "body", body.id, "in")
                elif spec.type_name.startswith("branch_"):
                    true_action = graph.add_node("wait")
                    graph.add_link(node.id, "true", true_action.id, "in")
                self.assertTrue(graph_to_script(graph))

    def test_round_trips_file_with_viewport(self) -> None:
        graph = new_graph()
        graph.viewport_x = 120.5
        graph.viewport_y = -44
        graph.viewport_zoom = 1.25
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.macrograph.json"
            save_graph(path, graph)
            loaded = load_graph(path)
        self.assertEqual(loaded.to_dict(), graph.to_dict())

    def test_script_converts_to_graph_and_back(self) -> None:
        source = '''
        OCR_NUMBER health 100 40 180 50
        IF_NUMBER health < 30
            PRESS q
        ELSE
            WAIT 0.2
        END
        REPEAT 2
            CLICK left 2 0.1
            TYPE "HP: ${health}" 0.02
        END
        '''
        graph = script_to_graph(source)
        rebuilt = graph_to_script(graph)
        expected = script_nodes_to_text(parse_script(source).nodes)
        self.assertEqual(rebuilt, expected)
        branch = next(node for node in graph.nodes if node.type == "branch_number")
        self.assertEqual(len(graph.incoming(branch.id, "value")), 1)

    def test_constant_data_node_can_feed_numeric_input(self) -> None:
        graph = GraphDocument()
        start = graph.add_node("start")
        number = graph.add_node("number_value", params={"value": 1.5})
        wait = graph.add_node("wait")
        graph.add_link(start.id, "out", wait.id, "in")
        graph.add_link(number.id, "value", wait.id, "seconds")
        self.assertEqual(graph_to_script(graph), "WAIT 1.5\n")

    def test_ocr_output_can_feed_branch(self) -> None:
        graph = GraphDocument()
        start = graph.add_node("start")
        ocr = graph.add_node("ocr_number", params={"variable": "score"})
        branch = graph.add_node(
            "branch_number",
            params={"operator": ">=", "expected": 10},
        )
        click = graph.add_node("click")
        graph.add_link(start.id, "out", ocr.id, "in")
        graph.add_link(ocr.id, "out", branch.id, "in")
        graph.add_link(ocr.id, "value", branch.id, "value")
        graph.add_link(branch.id, "true", click.id, "in")
        source = graph_to_script(graph)
        self.assertIn("OCR_NUMBER score", source)
        self.assertIn("IF_NUMBER score >= 10", source)

    def test_compiler_maps_source_lines_back_to_nodes(self) -> None:
        graph = script_to_graph("WAIT 0.2\nREPEAT 2\n    CLICK left\nEND\n")
        source, line_map = graph_to_script_with_line_map(graph)
        self.assertEqual(source.splitlines()[0], "WAIT 0.2")
        self.assertEqual(set(line_map), {1, 2, 3})
        self.assertEqual(
            {graph.node_map()[node_id].type for node_id in line_map.values()},
            {"wait", "repeat", "click"},
        )

    def test_rejects_cycles_in_execution_flow(self) -> None:
        graph = GraphDocument()
        start = graph.add_node("start")
        first = graph.add_node("wait")
        second = graph.add_node("wait")
        graph.add_link(start.id, "out", first.id, "in")
        graph.add_link(first.id, "out", second.id, "in")
        graph.add_link(second.id, "out", first.id, "in")
        with self.assertRaisesRegex(GraphError, "несколько связей|циклические"):
            graph.validate()

    def test_rejects_incompatible_and_duplicate_inputs(self) -> None:
        graph = GraphDocument()
        start = graph.add_node("start")
        text = graph.add_node("text_value")
        wait = graph.add_node("wait")
        graph.add_link(start.id, "out", wait.id, "in")
        graph.add_link(text.id, "value", wait.id, "seconds")
        with self.assertRaisesRegex(GraphError, "Нельзя соединить"):
            graph.validate()

        graph.links.pop()
        one = graph.add_node("number_value", params={"value": 1})
        two = graph.add_node("number_value", params={"value": 2})
        graph.add_link(one.id, "value", wait.id, "seconds")
        graph.add_link(two.id, "value", wait.id, "seconds")
        with self.assertRaisesRegex(GraphError, "несколько связей"):
            graph.validate()

    def test_rejects_non_finite_layout_values(self) -> None:
        graph = new_graph()
        graph.nodes[0].x = math.nan
        with self.assertRaisesRegex(GraphError, "Координаты узлов"):
            graph.validate()


if __name__ == "__main__":
    unittest.main()
