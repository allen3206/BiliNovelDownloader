"""純協定解析測試，不啟動子程序、不連網，也不寫入檔案。"""
import unittest
from pathlib import Path

from packer import PackerProtocolError, _PackerProtocol, _selected_indices


class PackerProtocolTests(unittest.TestCase):
    cwd = Path(__file__).resolve().parent
    url = "https://tw.linovelib.com/novel/1.html"

    def protocol(self, version="0.2.50", volumes="1", merge="2", title="1", legacy=False):
        protocol = _PackerProtocol(self.url, volumes, merge, title, self.cwd)
        protocol.feed(f"当前版本: {version}\n")
        prompt = ("请输入URL(支持哔哩轻小说&轻小说文库):" if legacy or version == "0.2.44"
                  else "请输入链接(多个链接使用空格隔开):")
        self.assertEqual(protocol.feed(prompt), [self.url])
        return protocol

    def select(self, protocol):
        prompt = ("请选择需要下载的分卷(可输入如1-9进行范围选择以及如2,5单独选择):"
                  if protocol.profile == "0.2.44" else "请选择要下载的分卷:")
        return protocol.feed("\n[ 1] 卷一,特别篇\n[ 2] 卷二\n[ 0] 选择全部\n" + prompt)

    def start_single(self, protocol):
        self.assertEqual(self.select(protocol), ["1"])
        self.assertEqual(protocol.feed("是否在每章开头添加章节标题?"), ["1"])

    def complete(self, protocol, name="volume.epub"):
        return protocol.feed(f"打包完成: {self.cwd / 'book' / name}\n")

    def test_single_volume_never_sends_merge_and_keeps_title(self):
        protocol = self.protocol(merge="2", title="1")
        self.start_single(protocol)
        self.complete(protocol)
        self.assertEqual(protocol.feed("请输入链接(多个链接使用空格隔开):"), [])
        self.assertEqual(protocol.boundary, "idle")
        self.assertEqual(protocol.expected_outputs, 1)

    def test_every_character_split_including_no_newline_prompts(self):
        protocol = _PackerProtocol(self.url, "1", "2", "1", self.cwd)
        transcript = ("当前版本: 0.2.50\n请输入链接(多个链接使用空格隔开):\n"
                      "[ 1] 卷一,特别篇\n[ 0] 选择全部\n请选择要下载的分卷: "
                      "\n是否在每章开头添加章节标题? \n"
                      f"打包完成: {self.cwd / 'book' / 'a.epub'}\n"
                      "请输入链接(多个链接使用空格隔开):")
        replies = []
        for character in transcript:
            replies.extend(protocol.feed(character))
        self.assertEqual(replies, [self.url, "1", "1"])
        self.assertEqual(protocol.menu[1], "卷一,特别篇")
        self.assertEqual(protocol.boundary, "idle")

    def test_merge_all_requires_one_output(self):
        protocol = self.protocol(volumes="0", merge="1", title="2")
        self.assertEqual(self.select(protocol), ["1,2"])
        self.assertEqual(protocol.feed("是否合并选择的分卷为一个文件?"), ["1"])
        self.assertEqual(protocol.feed("是否在每章开头添加章节标题?"), ["2"])
        self.complete(protocol)
        protocol.feed("请输入链接(多个链接使用空格隔开):")
        self.assertEqual(protocol.expected_outputs, 1)

    def test_partial_multi_volume_never_counts_as_success(self):
        protocol = self.protocol(volumes="1-2", merge="2")
        self.select(protocol)
        protocol.feed("是否合并选择的分卷为一个文件?")
        protocol.feed("是否在每章开头添加章节标题?")
        self.complete(protocol)
        with self.assertRaises(PackerProtocolError):
            protocol.feed("请输入链接(多个链接使用空格隔开):")

    def test_legacy_exit_prompt_gets_only_final_enter(self):
        protocol = self.protocol(version="0.2.44")
        self.start_single(protocol)
        self.complete(protocol)
        self.assertEqual(protocol.feed("全部任务已完成，按回车键退出."), [""])
        self.assertEqual(protocol.boundary, "exit")

    def test_legacy_fatal_marker_is_error_even_after_output(self):
        protocol = self.protocol(version="0.2.44")
        self.start_single(protocol)
        self.complete(protocol)
        with self.assertRaises(PackerProtocolError):
            protocol.feed("运行出错，按回车键退出.(0.2.44)\n")

    def test_duplicate_completed_path_is_error(self):
        protocol = self.protocol()
        self.start_single(protocol)
        self.complete(protocol)
        with self.assertRaises(PackerProtocolError):
            self.complete(protocol)

    def test_completed_path_must_stay_inside_work_directory(self):
        protocol = self.protocol()
        self.start_single(protocol)
        with self.assertRaises(PackerProtocolError):
            protocol.feed(f"打包完成: {self.cwd.parent / 'outside.epub'}\n")

    def test_missing_version_is_explicit_error(self):
        for transcript in ("当前版本: \n请输入链接(多个链接使用空格隔开):",
                           "请输入链接(多个链接使用空格隔开):"):
            with self.subTest(transcript=transcript):
                protocol = _PackerProtocol(self.url, "1", "2", "1", self.cwd)
                with self.assertRaises(PackerProtocolError):
                    protocol.feed(transcript)

    def test_changed_url_grammar_is_explicit_error(self):
        protocol = self.protocol(version="0.2.44")
        with self.assertRaises(PackerProtocolError):
            protocol.feed("请输入链接(多个链接使用空格隔开):")

    def test_unknown_version_warns_once_and_uses_current_format(self):
        with self.assertLogs(level="WARNING") as logs:
            protocol = self.protocol(version="9.9.9-test")
            protocol.feed("当前版本: 9.9.9-test\n")
            self.start_single(protocol)
            self.complete(protocol)
            self.assertEqual(protocol.feed("请输入链接(多个链接使用空格隔开):"), [])
        self.assertEqual(len(logs.output), 1)
        self.assertIn("9.9.9-test", logs.output[0])
        self.assertIn("尚未實測", logs.output[0])
        self.assertEqual(protocol.boundary, "idle")

    def test_unknown_version_can_use_legacy_format(self):
        protocol = self.protocol(version="9.9.9", legacy=True)
        self.start_single(protocol)
        self.complete(protocol)
        self.assertEqual(protocol.feed("全部任务已完成，按回车键退出."), [""])
        self.assertEqual(protocol.boundary, "exit")

    def test_actual_format_not_known_version_selects_profile(self):
        protocol = self.protocol(legacy=True)
        self.start_single(protocol)
        self.complete(protocol)
        protocol.feed("请输入URL(支持哔哩轻小说&轻小说文库):")
        self.assertEqual(protocol.boundary, "idle")

    def test_mixed_volume_format_is_rejected(self):
        protocol = self.protocol(version="9.9.9")
        with self.assertRaises(PackerProtocolError):
            protocol.feed("[1] 卷一\n[0] 选择全部\n"
                          "请选择需要下载的分卷(可输入如1-9进行范围选择以及如2,5单独选择):")

    def test_unknown_prompt_never_gets_an_answer(self):
        protocol = self.protocol(version="9.9.9")
        with self.assertRaisesRegex(PackerProtocolError, "未知互動提示"):
            protocol.feed("请选择输出格式:")

    def test_unknown_version_error_after_output_is_failure(self):
        protocol = self.protocol(version="9.9.9")
        self.start_single(protocol)
        self.complete(protocol)
        with self.assertRaises(PackerProtocolError):
            protocol.feed("运行出错.(9.9.9)\n请输入链接(多个链接使用空格隔开):")

    def test_unknown_completion_text_does_not_finish(self):
        protocol = self.protocol(version="9.9.9")
        self.start_single(protocol)
        self.complete(protocol)
        self.assertEqual(protocol.feed("所有文件处理完毕\n", final=True), [])
        self.assertIsNone(protocol.boundary)

    def test_version_change_during_job_is_rejected(self):
        protocol = self.protocol(version="9.9.9")
        with self.assertRaises(PackerProtocolError):
            protocol.feed("当前版本: 9.9.10\n")

    def test_unexpected_single_volume_merge_is_error(self):
        protocol = self.protocol()
        self.select(protocol)
        with self.assertRaises(PackerProtocolError):
            protocol.feed("是否合并选择的分卷为一个文件?")

    def test_invalid_range_is_rejected_before_input(self):
        for value in ("-1", "0,1", "3", "1-999999999", "1,a", "1,", "1--2"):
            with self.subTest(value=value), self.assertRaises(PackerProtocolError):
                _selected_indices(value, {1: "卷一", 2: "卷二"})

    def test_selection_normalizes_reversed_range_and_deduplicates(self):
        self.assertEqual(_selected_indices("3 - 1，2", {1: "甲", 2: "乙", 3: "丙"}), [1, 2, 3])

    def test_prompt_quoted_in_volume_name_or_log_is_not_input(self):
        protocol = self.protocol()
        phrase = "是否在每章开头添加章节标题?"
        self.assertEqual(protocol.feed(f"[1] {phrase}\n[2] 第二卷\n[0] 选择全部\n"), [])
        self.assertEqual(protocol.menu[1], phrase)
        self.assertEqual(protocol.feed(f"描述：{phrase}\n"), [])
        self.assertEqual(protocol.feed(f"[INFO] {phrase}\n"), [])
        self.assertEqual(protocol.feed(f"[INFO] {phrase}"), [])
        self.assertEqual(protocol.feed("\n请选择要下载的分卷:"), ["1"])
        self.assertEqual(protocol.feed(f"[INFO] {phrase}\n"), [])
        self.assertEqual(protocol.phase, "title")
        self.assertEqual(protocol.feed("  " + phrase + "  "), ["1"])

    def test_quoted_url_prompt_does_not_finish_job(self):
        protocol = self.protocol()
        self.start_single(protocol)
        self.complete(protocol)
        self.assertEqual(protocol.feed("[INFO] 请输入链接(多个链接使用空格隔开):\n"), [])
        self.assertEqual(protocol.feed("描述包含 请输入链接(多个链接使用空格隔开):"), [])
        self.assertIsNone(protocol.boundary)
        self.assertEqual(protocol.feed("\n请输入链接(多个链接使用空格隔开):"), [])
        self.assertEqual(protocol.boundary, "idle")

    def test_quoted_fatal_text_is_not_core_failure(self):
        protocol = self.protocol()
        self.assertEqual(protocol.feed("[1] 运行出错.(0.2.50)\n[0] 选择全部\n"), [])
        self.assertEqual(protocol.feed("描述：运行出错.(0.2.50)\n"), [])
        self.assertEqual(protocol.feed("[INFO] 运行出错.(0.2.50)"), [])


if __name__ == "__main__":
    unittest.main()
