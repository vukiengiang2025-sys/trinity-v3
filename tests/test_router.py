from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import make_config

from core.router import Router, RouterError, normalize


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.router = Router(make_config(Path(self.tmp.name)))

    def test_memory_route(self):
        route = self.router.route("ghi nhớ thông tin này vào memory")
        self.assertEqual(route.task_type, "MEMORY")
        self.assertEqual(route.selected_adapter, "hermes")
        self.assertEqual(route.execution_plan[0]["action"], "memory_store")

    def test_memory_query_route(self):
        route = self.router.route("truy xuất memory về dự án cũ")
        self.assertEqual(route.task_type, "MEMORY")
        self.assertEqual(route.execution_plan[0]["action"], "memory_query")

    def test_coding_route(self):
        route = self.router.route("sửa lỗi pytest trong core/router.py")
        self.assertEqual(route.task_type, "CODING")
        self.assertEqual(route.selected_adapter, "opencode")
        self.assertEqual(route.execution_plan[0]["adapter"], "opencode")

    def test_automation_route(self):
        route = self.router.route("chạy lệnh git status và backup thư mục")
        self.assertEqual(route.task_type, "AUTOMATION")
        self.assertEqual(route.selected_adapter, "openclaw")

    def test_general_route(self):
        route = self.router.route("xin chào, hôm nay thế nào")
        self.assertEqual(route.task_type, "GENERAL")
        self.assertEqual(route.selected_adapter, "opencode")
        self.assertEqual(len(route.execution_plan), 1)
        self.assertEqual(route.execution_plan[0]["adapter"], "opencode")

    def test_negated_shell_is_not_automation(self):
        route = self.router.route(
            "Đọc sample.py và tóm tắt hàm chính. Không sửa file, không chạy shell."
        )
        self.assertEqual(route.task_type, "CODING")
        self.assertEqual([step["adapter"] for step in route.execution_plan], ["opencode"])

    def test_negated_memory_is_not_memory(self):
        route = self.router.route("Không dùng memory; chỉ giải thích python.")
        self.assertEqual(route.task_type, "CODING")
        self.assertEqual([step["adapter"] for step in route.execution_plan], ["opencode"])

    def test_mixed_route_is_sequential_and_ordered(self):
        route = self.router.route("đọc memory, refactor code rồi chạy lệnh git status")
        self.assertEqual(route.task_type, "MIXED")
        self.assertEqual(route.selected_adapter, "multi")
        adapters = [step["adapter"] for step in route.execution_plan]
        self.assertEqual(adapters, ["hermes", "opencode", "openclaw"])
        self.assertEqual([step["step"] for step in route.execution_plan], ["1", "2", "3"])

    def test_high_risk_classification(self):
        route = self.router.route("xóa toàn bộ dữ liệu production")
        self.assertEqual(route.risk_level, "high")
        self.assertTrue(route.approval_required)

    def test_medium_risk_coding(self):
        route = self.router.route("refactor module python")
        self.assertIn(route.risk_level, ("medium", "high"))

    def test_task_id_is_unique_and_prefixed(self):
        first = self.router.route("xin chào")
        second = self.router.route("xin chào")
        self.assertTrue(first.task_id.startswith("TRN-"))
        self.assertTrue(second.task_id.startswith("TRN-"))
        self.assertNotEqual(first.task_id, second.task_id)

    def test_empty_prompt_rejected(self):
        with self.assertRaises(RouterError):
            self.router.route("   ")

    def test_normalize_removes_vietnamese_accents(self):
        self.assertEqual(normalize("Sửa lỗi ĐỌC file"), "sua loi doc file")


if __name__ == "__main__":
    unittest.main()
