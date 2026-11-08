"""封面 HTTP、快取與原生 Tk 執行緒交接測試，不連線至外部網站。"""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image
import requests
import gui
import scraper


class CoverDownloadTests(unittest.TestCase):
    page = 'https://tw.linovelib.com/novel/2979.html'
    url = 'https://fixture.invalid/cover.jpg'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cover-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = patch.object(scraper, 'get_base_path', return_value=self.root)
        self.base.start()
        self.addCleanup(self.base.stop)
        self.client = scraper.NovelScraper()
        stream = io.BytesIO()
        Image.new('RGB', (400, 588), 'blue').save(stream, 'JPEG')
        self.jpeg = stream.getvalue()
        self.cache = self.root/'temp/covers'/(hashlib.md5(self.url.encode()).hexdigest()+'.jpg')

    def fetch(self):
        return self.client.download_cover(self.url, referer=self.page)

    def test_success_referer_is_per_request_and_thumbnail_is_valid(self):
        original = dict(self.client.headers)
        with patch.object(scraper.requests, 'get', return_value=Mock(status_code=200, content=self.jpeg)) as get:
            image = self.fetch()
        self.assertEqual(get.call_args.kwargs['headers']['Referer'], self.page)
        self.assertEqual(self.client.headers, original)
        self.assertLessEqual(image.width, 200)
        self.assertLessEqual(image.height, 280)
        with Image.open(self.cache) as cached:
            cached.load()
            self.assertEqual(cached.size, (400, 588))

    def test_http_403_is_logged_and_not_cached(self):
        with patch.object(scraper.requests, 'get', return_value=Mock(status_code=403)), self.assertLogs(level='WARNING') as logs:
            self.assertIsNone(self.fetch())
        self.assertIn('403', ''.join(logs.output))
        self.assertFalse(self.cache.exists())

    def test_timeout_returns_failure(self):
        with patch.object(scraper.requests, 'get', side_effect=requests.Timeout('fixture timeout')), self.assertLogs(level='WARNING'):
            self.assertIsNone(self.fetch())

    def test_200_non_image_returns_failure(self):
        with patch.object(scraper.requests, 'get', return_value=Mock(status_code=200, content=b'<html>error</html>')), self.assertLogs(level='WARNING'):
            self.assertIsNone(self.fetch())
        self.assertFalse(self.cache.exists())

    def test_missing_url_does_not_request(self):
        with patch.object(scraper.requests, 'get') as get:
            self.assertIsNone(self.client.download_cover(''))
        get.assert_not_called()

    def test_valid_cache_does_not_request(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_bytes(self.jpeg)
        with patch.object(scraper.requests, 'get') as get:
            self.assertIsNotNone(self.fetch())
        get.assert_not_called()

    def test_corrupt_cache_is_refetched(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_bytes(b'broken')
        with patch.object(scraper.requests, 'get', return_value=Mock(status_code=200, content=self.jpeg)) as get:
            self.assertIsNotNone(self.fetch())
        get.assert_called_once()

    def test_expired_cache_is_refetched(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_bytes(self.jpeg)
        os.utime(self.cache, (time.time()-90000, time.time()-90000))
        with patch.object(scraper.requests, 'get', return_value=Mock(status_code=200, content=self.jpeg)) as get:
            self.assertIsNotNone(self.fetch())
        get.assert_called_once()


class CoverTkTests(unittest.TestCase):
    def run_case(self, *, missing=False, failure=False, raised=False, photo_failure=False,
                 catalog_failure=False, auto_start=False):
        with tempfile.TemporaryDirectory(prefix='cover-tk-') as directory, \
             patch.object(gui, 'get_base_path', return_value=Path(directory)), \
             patch.object(gui.Application, 'setup_logging', lambda self: None), \
             patch.object(gui.Application, '_start_update_check', lambda self: None):
            app = gui.Application()
            app.withdraw()
            app.reset_info_labels()
            app.set_app_state(gui.AppState.CHECKING)
            info = scraper.NovelInfo(url=CoverDownloadTests.page,
                                     cover_url=None if missing else CoverDownloadTests.url)
            app.scraper = Mock()
            app.scraper.fetch_info.return_value = info
            app.scraper.download_cover.return_value = None if failure else Image.new('RGB', (100, 140))
            if raised:
                app.scraper.download_cover.side_effect = OSError('fixture read error')
            app.scraper.fetch_catalog.return_value = [('1', 'fixture volume')]
            if catalog_failure:
                app.scraper.fetch_catalog.side_effect = ValueError('fixture catalog error')
            started = []
            app.start_process = lambda: started.append(True)
            main_thread = threading.get_ident()
            photo_threads = []
            original_photo = gui.ImageTk.PhotoImage

            def photo(*args, **kwargs):
                photo_threads.append(threading.get_ident())
                if photo_failure:
                    raise RuntimeError('fixture Tk image error')
                return original_photo(*args, **kwargs)

            worker = threading.Thread(target=app._thread_fetch_info,
                                      args=(info.url, auto_start), daemon=True)
            deadline = time.monotonic()+5
            expired = []

            def poll():
                if not worker.is_alive() and app.app_state == gui.AppState.IDLE and (not auto_start or started):
                    app.quit()
                elif time.monotonic() > deadline:
                    expired.append(True)
                    app.quit()
                else:
                    app.after(20, poll)

            try:
                with patch.object(gui.ImageTk, 'PhotoImage', side_effect=photo):
                    worker.start()
                    app.after(20, poll)
                    app.mainloop()
                worker.join(timeout=1)
                self.assertFalse(expired or worker.is_alive())
                self.assertNotIn('正在載入', app.cover_label.cget('text'))
                self.assertTrue(all(t == main_thread for t in photo_threads))
                unsuccessful = missing or failure or raised or photo_failure
                if unsuccessful:
                    self.assertTrue(app.cover_label.cget('text'))
                    self.assertFalse(app.cover_label.cget('image'))
                    self.assertIsNone(app.cover_label.image)
                    self.assertIn('封面', app.status_label.cget('text'))
                    self.assertEqual(str(app.status_label.cget('foreground')), 'orange')
                else:
                    self.assertEqual(app.cover_label.cget('text'), '')
                    self.assertTrue(app.cover_label.cget('image'))
                    self.assertIsNotNone(app.cover_label.image)
                    self.assertEqual(photo_threads, [main_thread])
                if missing:
                    app.scraper.download_cover.assert_not_called()
                else:
                    app.scraper.download_cover.assert_called_once_with(info.cover_url, referer=info.url)
                if not catalog_failure:
                    self.assertEqual(len(app.vol_tree_id.get_children()), 1)
                if auto_start:
                    self.assertEqual(started, [True])
                app.reset_info_labels()
                self.assertIsNone(app._cover_error)
            finally:
                for callback in app.tk.call('after', 'info'):
                    # 子元件持有部分 callback；先取消計時，再交由 destroy() 刪除註冊
                    app.tk.call('after', 'cancel', callback)
                app.destroy()

    def test_success_creates_photo_on_main_thread_and_keeps_reference(self):
        self.run_case()

    def test_missing_cover_has_terminal_state(self):
        self.run_case(missing=True)

    def test_cover_failure_survives_catalog_success(self):
        self.run_case(failure=True)

    def test_cover_exception_has_terminal_state(self):
        self.run_case(raised=True)

    def test_photo_creation_failure_has_terminal_state(self):
        self.run_case(photo_failure=True)

    def test_cover_and_catalog_failure_preserve_warning(self):
        self.run_case(failure=True, catalog_failure=True)

    def test_cover_failure_does_not_block_auto_start(self):
        self.run_case(failure=True, auto_start=True)


if __name__ == '__main__':
    unittest.main()
