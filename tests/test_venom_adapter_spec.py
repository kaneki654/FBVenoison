"""The suite build adds its hook without replacing upstream packaging behavior."""
import builtins
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / 'FBVenom-suite.spec').read_text()
UPSTREAM = (ROOT / 'FBVenom/FBVenom.spec').read_bytes()


def pe_stub():
    raw = bytearray(132)
    raw[:2] = b'MZ'
    raw[60:64] = (128).to_bytes(4, 'little')
    raw[128:] = b'PE\0\0'
    return bytes(raw)


class AdapterSpecTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vendor = self.root / 'FBVenom'
        self.vendor.mkdir()
        (self.vendor / 'FBVenom.spec').write_bytes(UPSTREAM)
        for folder, names in [('platform-tools', ['adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll']),
                              ('gnirehtet', ['gnirehtet.exe'])]:
            directory = self.vendor / folder
            directory.mkdir()
            for name in names:
                (directory / name).write_bytes(pe_stub())
        self.output = self.root / 'output/FBVenom.exe'
        self.output.parent.mkdir()
        self.output.write_bytes(b'completed test build')
        self.analysis = Mock(return_value=SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[]))

    def evaluate(self, platform):
        def verify(directory, platform=None):
            directory = Path(directory)
            return {'relay': str(directory / 'gnirehtet.exe')}

        def imports(name, *args, **kwargs):
            if name == 'sys':
                return SimpleNamespace(platform=platform, path=[])
            if name == 'PyInstaller.utils.hooks':
                return SimpleNamespace(collect_all=lambda _: ([], [], []))
            if name == 'tools_prepare_usb_internet':
                return SimpleNamespace(verify_assets=verify)
            return builtins.__import__(name, *args, **kwargs)

        from unittest.mock import patch
        namespace = {'SPECPATH': str(self.root), 'SPEC': str(self.root / 'FBVenom-suite.spec'),
                     'Analysis': self.analysis, 'PYZ': Mock(),
                     'EXE': Mock(return_value=SimpleNamespace(name=str(self.output))),
                     '__builtins__': dict(vars(builtins), __import__=imports)}
        with patch.dict('os.environ'):
            import os
            os.environ.pop('FBVENOM_PLATFORM_TOOLS', None)
            os.environ.pop('FBVENOM_GNIREHTET_DIR', None)
            exec(compile(SOURCE, 'FBVenom-suite.spec', 'exec'), namespace)
        return namespace

    def test_upstream_graph_and_assets_are_preserved_with_external_hook(self):
        import sys
        sys.path.insert(0, str(ROOT))
        self.addCleanup(lambda: sys.path.remove(str(ROOT)))
        namespace = self.evaluate('win32')
        args = self.analysis.call_args
        self.assertEqual(args.args[0], [str(self.vendor / 'fbvenom.py')])
        self.assertEqual(args.kwargs['pathex'][:2], [str(self.vendor), str(self.root)])
        self.assertIn(str(self.root / 'integration/venom_runtime_hook.py'), args.kwargs['runtime_hooks'])
        self.assertIn('venom_bridge', args.kwargs['hiddenimports'])
        bundled = {Path(path).name for path, _ in args.kwargs['datas']}
        self.assertTrue({'icon.png', 'icon.ico', 'icon_nobg.png', 'gnirehtet.apk', 'LICENSE', 'provenance.json'} <= bundled)
        binaries = {Path(path).name for path, _ in args.kwargs['binaries']}
        self.assertEqual(binaries, {'adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'gnirehtet.exe'})
        self.assertEqual((self.vendor / 'FBVenom.spec').read_bytes(), UPSTREAM)
        self.assertFalse(namespace['upstream_namespace']['EXE'].call_args.kwargs['upx'])
        import venom_bridge
        marker = self.output.parent / venom_bridge.METADATA_NAME
        self.assertTrue(marker.is_file())
        import hashlib, json
        self.assertEqual(json.loads(marker.read_text())['sha256'], hashlib.sha256(self.output.read_bytes()).hexdigest())

    def test_non_windows_build_is_rejected_before_packaging_or_marker(self):
        with self.assertRaisesRegex(RuntimeError, 'Windows Python'):
            self.evaluate('linux')
        self.analysis.assert_not_called()
        self.assertFalse((self.output.parent / 'FBVenom.suite-bridge.json').exists())


if __name__ == '__main__':
    unittest.main()
