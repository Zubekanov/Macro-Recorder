import unittest

from macro_recorder.undo import UndoStack


class TestUndoStack(unittest.TestCase):
    def test_empty_stack_has_nothing_to_do(self):
        s = UndoStack()
        self.assertIsNone(s.undo("now"))
        self.assertIsNone(s.redo("now"))
        self.assertFalse(s.can_undo)
        self.assertFalse(s.can_redo)

    def test_undo_then_redo_round_trip(self):
        s = UndoStack()
        s.push("a")          # state before editing to "b"
        self.assertEqual(s.undo("b"), "a")
        self.assertTrue(s.can_redo)
        self.assertEqual(s.redo("a"), "b")
        self.assertFalse(s.can_redo)
        self.assertTrue(s.can_undo)

    def test_push_clears_redo(self):
        s = UndoStack()
        s.push("a")
        s.undo("b")
        s.push("c")
        self.assertFalse(s.can_redo)
        self.assertEqual(s.undo("d"), "c")

    def test_multiple_steps_come_back_in_order(self):
        s = UndoStack()
        for state in ("a", "b", "c"):
            s.push(state)
        self.assertEqual(s.undo("d"), "c")
        self.assertEqual(s.undo("c"), "b")
        self.assertEqual(s.undo("b"), "a")
        self.assertIsNone(s.undo("a"))
        self.assertEqual(s.redo("a"), "b")

    def test_limit_drops_oldest(self):
        s = UndoStack(limit=2)
        for state in ("a", "b", "c"):
            s.push(state)
        self.assertEqual(s.undo("d"), "c")
        self.assertEqual(s.undo("c"), "b")
        self.assertIsNone(s.undo("b"))

    def test_peek_and_clear(self):
        s = UndoStack()
        self.assertIsNone(s.peek())
        s.push("a")
        self.assertEqual(s.peek(), "a")
        s.clear()
        self.assertIsNone(s.peek())
        self.assertFalse(s.can_undo)


if __name__ == "__main__":
    unittest.main()
