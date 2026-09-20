import contextlib
import io
import threading
import unittest
from unittest.mock import Mock, patch
import ajazz_battery as app

class MonitorTests(unittest.TestCase):
    def monitor(self):
        with contextlib.redirect_stdout(io.StringIO()):
            m = app.TrayMonitor(Mock(), 120, 20, 3600)
        m.icon = Mock()
        m._notify = Mock()
        m._refresh_icon = Mock()
        return m

    def test_captured_packet(self):
        h = Mock()
        app.send_clear_screen(h)
        expected = b'\x00' + bytes.fromhex('ac 00 00 00 00 00 00 53') + bytes(56)
        h.send_feature_report.assert_called_once_with(expected)

    def test_invalid_status(self):
        h = Mock()
        for response in (b'', bytes(4), bytes([0, 0, 0, 255, 0])):
            h.get_feature_report.return_value = response
            self.assertIsNone(app.read_status(h)[0])
        for p in (0, 50, 100):
            h.get_feature_report.return_value = bytes([0, 0, 0, p, 0])
            self.assertEqual(app.read_status(h)[0], p)

    def test_poll_error_clears_stale_and_closes(self):
        m = self.monitor()
        m.percent = 80
        h = Mock()
        with patch.object(app, 'open_working_device', return_value=(h, {})), patch.object(app, 'read_status', return_value=(None, None, b'')):
            m._poll_once()
        self.assertIsNone(m.percent)
        self.assertIsNotNone(m.last_error)
        h.close.assert_called_once()
        m._refresh_icon.assert_called_once()

    def test_clear_failure_unlocks_and_closes(self):
        m = self.monitor()
        h = Mock()
        def fail(handle):
            self.assertTrue(m.hid_lock.locked())
            raise OSError('disconnected')
        with patch.object(app, 'open_working_device', return_value=(h, {})), patch.object(app, 'send_clear_screen', side_effect=fail):
            m._clear_screen_manual()
        h.close.assert_called_once()
        self.assertFalse(m.hid_lock.locked())
        self.assertIn('Не удалось', m._notify.call_args.args[1])

    def test_manual_nonblocking_and_no_duplicate(self):
        m = self.monitor()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def action():
            calls.append(1)
            entered.set()
            release.wait(2)
            finished.set()
        try:
            m._start_manual(action)
            self.assertTrue(entered.wait(1))
            m._start_manual(action)
            self.assertEqual(calls, [1])
        finally:
            release.set()
        self.assertTrue(finished.wait(1))
        self.assertTrue(m.manual_lock.acquire(timeout=1))
        m.manual_lock.release()

    def test_resume_retries(self):
        m = self.monitor()
        m.stop_event = Mock()
        m.stop_event.is_set.return_value = False
        m.stop_event.wait.return_value = False
        m._do_sync_time = Mock(side_effect=[False, False, True])
        m._sync_after_resume()
        self.assertEqual(m._do_sync_time.call_count, 3)
        self.assertEqual(m.stop_event.wait.call_count, 2)

    def test_resume_gap(self):
        m = self.monitor()
        m.stop_event.wait = Mock(side_effect=[False, False, True])
        m._sync_after_resume = Mock()
        with patch.object(app.time, 'monotonic', side_effect=[100,105,125,126]):
            m._resume_watch_loop()
        m._sync_after_resume.assert_called_once()

    def test_cli_invalid_ranges(self):
        with patch.object(app, 'cmd_monitor') as start, contextlib.redirect_stderr(io.StringIO()):
            for args in (['--interval','0'], ['--sync-interval','-1'], ['--threshold','101'], ['--threshold','-1']):
                with self.assertRaises(SystemExit) as result:
                    app.main(['monitor'] + args)
                self.assertEqual(result.exception.code, 2)
            start.assert_not_called()

    def test_cli_handle_closed_on_error(self):
        h=Mock()
        with patch.object(app, '_import_hid'), patch.object(app, 'open_working_device', return_value=(h, {'path':b'test'})), patch.object(app, 'read_status', side_effect=OSError('lost')):
            with self.assertRaises(OSError):
                app.cmd_check()
        h.close.assert_called_once()

    def test_stopped_monitor_does_not_open_device(self):
        m=self.monitor()
        m.stop_event.set()
        with patch.object(app, 'open_working_device') as opened:
            m._poll_once()
            m._do_sync_time()
            m._clear_screen_manual()
            opened.assert_not_called()

if __name__ == '__main__':
    unittest.main()
