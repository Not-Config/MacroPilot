import os
import unittest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    from qt_graph import GraphEditor, GraphScene
except ImportError:
    QT_AVAILABLE = False
else:
    QT_AVAILABLE = True


@unittest.skipUnless(QT_AVAILABLE, "PySide6 is not installed")
class QtGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_editor_starts_with_compilable_graph(self) -> None:
        editor = GraphEditor()
        self.assertEqual(editor.to_source(), "WAIT 0\n")
        self.assertEqual(len(editor.scene.node_items), 2)
        editor.deleteLater()

    def test_scene_connects_typed_ports_and_replaces_exec_target(self) -> None:
        scene = GraphScene()
        start = next(item for item in scene.node_items.values() if item.node.type == "start")
        old_wait = next(item for item in scene.node_items.values() if item.node.type == "wait")
        click = scene.add_node("click", QPointF(600, 0))

        self.assertTrue(
            scene.connect_ports(old_wait.output_ports["out"], click.input_ports["in"])
        )
        self.assertEqual(len(scene.document.outgoing(old_wait.node.id, "out")), 1)

        self.assertTrue(
            scene.connect_ports(start.output_ports["out"], click.input_ports["in"])
        )
        self.assertFalse(scene.document.incoming(old_wait.node.id, "in"))
        self.assertEqual(scene.document.incoming(click.node.id, "in")[0].from_node, start.node.id)

    def test_code_round_trip_keeps_nested_graph(self) -> None:
        source = """
        OCR_NUMBER hp 10 20 100 40
        IF_NUMBER hp < 30
            PRESS q
        ELSE
            WAIT 0.2
        END
        """
        editor = GraphEditor()
        editor.load_source(source)
        rebuilt = editor.to_source()
        self.assertIn("OCR_NUMBER hp", rebuilt)
        self.assertIn("IF_NUMBER hp < 30", rebuilt)
        editor.deleteLater()

    def test_undo_and_redo_restore_graph_state(self) -> None:
        editor = GraphEditor()
        original_count = len(editor.document.nodes)
        editor.scene.add_node("click", QPointF(500, 100))
        editor._commit_history()
        self.assertEqual(len(editor.document.nodes), original_count + 1)
        editor.undo()
        self.assertEqual(len(editor.document.nodes), original_count)
        editor.redo()
        self.assertEqual(len(editor.document.nodes), original_count + 1)
        editor.deleteLater()


if __name__ == "__main__":
    unittest.main()
