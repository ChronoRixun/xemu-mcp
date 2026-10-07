import ctypes
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
from xemu_mcp.windows import capture_region, post_key


@unittest.skipUnless(sys.platform == 'win32', 'Windows fallback')
class RegionCaptureTests(unittest.TestCase):
    def test_targeted_keys_use_sdl_virtual_key_path(self):
        api, gui, process = Mock(), Mock(), Mock()
        api.MapVirtualKey.return_value = 0x1e
        process.GetWindowThreadProcessId.return_value = (10, 123)
        with patch('xemu_mcp.windows._apis', return_value=(api, gui, process)):
            post_key(42, 4, True, 123)
            post_key(42, 4, False, 123)
        self.assertEqual([call.args for call in gui.SendMessageTimeout.call_args_list],
                         [(42, 0x100, 0x41, 1, 2, 1000),
                          (42, 0x101, 0x41, 1 | 3 << 30, 2, 1000)])

    def test_reused_window_cannot_receive_a_key(self):
        gui = Mock()
        process = Mock()
        process.GetWindowThreadProcessId.return_value = (10, 456)
        with patch('xemu_mcp.windows._apis', return_value=(Mock(), gui, process)):
            with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                post_key(42, 4, True, 123)
        gui.SendMessageTimeout.assert_not_called()

    def test_accepted_but_delayed_raise_is_verified_and_restored(self):
        gui = Mock()
        gui.IsIconic.return_value = False
        gui.GetWindowLong.side_effect = [0x100, 0x100, 0x108]
        gui.GetClientRect.return_value = (0, 0, 40, 30)
        gui.ClientToScreen.return_value = (10, 20)
        gui.WindowFromPoint.return_value = 42
        user32 = Mock()
        user32.SetThreadDpiAwarenessContext.return_value = 123
        user32.SetWindowPos.return_value = 1
        with tempfile.TemporaryDirectory() as tmp:
            filename = Path(tmp) / 'synthetic.png'
            with patch('xemu_mcp.windows._apis', return_value=(Mock(), gui, Mock())), \
                 patch('xemu_mcp.windows.window_for_pid', return_value=42), \
                 patch.object(ctypes.windll, 'user32', user32), \
                 patch('xemu_mcp.windows.time.sleep'), \
                 patch('PIL.ImageGrab.grab', return_value=Image.new('RGB', (40, 30), (12, 34, 56))):
                result = capture_region(99, str(filename), bring_to_front=True)
            self.assertEqual(result['width'], 40)
            with Image.open(filename) as image:
                self.assertEqual(image.getpixel((0, 0)), (12, 34, 56))
        self.assertEqual(user32.SetWindowPos.call_count, 3)  # two attempts + restore
        self.assertEqual(user32.SetWindowPos.call_args.args[1].value, ctypes.c_void_p(-2).value)

    def test_obscured_window_fails_before_grabbing_pixels(self):
        gui = Mock()
        gui.IsIconic.return_value = False
        gui.GetClientRect.return_value = (0, 0, 40, 30)
        gui.ClientToScreen.return_value = (10, 20)
        gui.WindowFromPoint.return_value = 666
        gui.IsChild.return_value = False
        gui.GetWindowText.return_value = 'Invented obstruction'
        user32 = Mock()
        user32.SetThreadDpiAwarenessContext.return_value = 123
        with patch('xemu_mcp.windows._apis', return_value=(Mock(), gui, Mock())), \
             patch('xemu_mcp.windows.window_for_pid', return_value=42), \
             patch.object(ctypes.windll, 'user32', user32), \
             patch('PIL.ImageGrab.grab') as grab:
            with self.assertRaisesRegex(RuntimeError, 'Invented obstruction'):
                capture_region(99, 'unused.png')
        grab.assert_not_called()


if __name__ == '__main__':
    unittest.main()
