"""Issue #2 的成品完整性與啟動前失敗處理測試。"""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import packer


class PackerValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='issue2-validation-')
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.path = self.work / 'book.epub'
        self.members = {
            'mimetype': b'application/epub+zip',
            'META-INF/container.xml': b'<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
            'book.opf': b'<package><manifest><item id="chapter" href="chapter.xhtml"/></manifest><spine><itemref idref="chapter"/></spine></package>',
            'chapter.xhtml': b'<html><body>unique-fixture-text</body></html>',
        }

    def write_archive(self):
        with zipfile.ZipFile(self.path, 'w', compression=zipfile.ZIP_STORED) as archive:
            for name, data in self.members.items():
                archive.writestr(name, data)

    def test_valid_closed_archive_passes(self):
        self.write_archive()
        packer._validate_epub(self.path)

    def test_missing_and_empty_file_are_rejected(self):
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)
        self.path.touch()
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)

    def test_wrong_mimetype_is_rejected(self):
        self.members['mimetype'] = b'application/zip'
        self.write_archive()
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)

    def test_container_without_rootfile_is_rejected(self):
        self.members['META-INF/container.xml'] = b'<container><rootfiles/></container>'
        self.write_archive()
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)

    def test_missing_opf_member_is_rejected(self):
        del self.members['book.opf']
        self.write_archive()
        with self.assertRaises(KeyError):
            packer._validate_epub(self.path)

    def test_wrong_opf_root_is_rejected(self):
        self.members['book.opf'] = b'<not-a-package/>'
        self.write_archive()
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)

    def test_missing_spine_is_rejected(self):
        self.members['book.opf'] = b'<package><manifest><item id="chapter" href="chapter.xhtml"/></manifest></package>'
        self.write_archive()
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)

    def test_unresolved_spine_reference_is_rejected(self):
        self.members['book.opf'] = self.members['book.opf'].replace(b'idref="chapter"', b'idref="missing"')
        self.write_archive()
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)

    def test_corrupted_member_crc_is_rejected(self):
        self.write_archive()
        raw = self.path.read_bytes()
        self.assertEqual(raw.count(b'unique-fixture-text'), 1)
        self.path.write_bytes(raw.replace(b'unique-fixture-text', b'broken-fixture-text'))
        with self.assertRaises(ValueError):
            packer._validate_epub(self.path)

    def test_cancelled_before_start_does_not_spawn_child(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(packer.subprocess, 'Popen') as spawn:
            result = packer.run_interactive_packer(self.work/'core.exe', 'https://fixture.invalid/book',
                                                  '1', '2', '1', self.work, cancel)
        spawn.assert_not_called()
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'cancelled')
        self.assertFalse((self.work/'packer.pid').exists())

    def test_spawn_failure_is_reported_without_pid(self):
        with patch.object(packer.subprocess, 'Popen', side_effect=FileNotFoundError('missing core')):
            result = packer.run_interactive_packer(self.work/'core.exe', 'https://fixture.invalid/book',
                                                  '1', '2', '1', self.work, threading.Event())
        self.assertFalse(result.success)
        self.assertEqual(result.reason, 'error')
        self.assertIsNone(result.returncode)
        self.assertIn('missing core', result.message)
        self.assertFalse((self.work/'packer.pid').exists())


if __name__ == '__main__':
    unittest.main()
