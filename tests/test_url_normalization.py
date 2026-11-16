"""The two child apps must treat the same Facebook target identically."""

import contextlib
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "FBVenom"))
sys.path.insert(0, str(ROOT / "FBPoison"))


def _load_core(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VENOM = _load_core(ROOT / "FBVenom" / "core.py", "fbvenom_url_test_core")
POISON = _load_core(ROOT / "FBPoison" / "core.py", "fbpoison_url_test_core")


class _FakeResponse:
    def __init__(self, final_url, body=""):
        self._final_url = final_url
        self._body = body.encode("utf-8")
        self._pos = 0

    def geturl(self):
        return self._final_url

    def read(self, limit=None):
        end = None if limit is None else self._pos + limit
        chunk = self._body[self._pos:end]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@contextlib.contextmanager
def _fake_network(core, final_url, body):
    class _Opener:
        def open(self, req, timeout=None):
            return _FakeResponse(final_url, body)

    previous = core.NOPROXY
    core.NOPROXY = _Opener()
    core._url_resolve_cache.clear()
    try:
        yield
    finally:
        core.NOPROXY = previous
        core._url_resolve_cache.clear()


class FacebookShareVideoTests(unittest.TestCase):
    SAMPLE = "https://www.facebook.com/share/v/19zVwRPKRW/"
    CLEAN_SHARE = "https://www.facebook.com/share/v/19zVwRPKRW"
    CANONICAL = "https://www.facebook.com/reel/42"
    HTML = ('<head><link rel="canonical" '
            'href="https://www.facebook.com/reel/42/" /></head>')

    def test_both_apps_normalize_and_resolve_share_v(self):
        for core in (VENOM, POISON):
            with self.subTest(core=core.__name__):
                self.assertEqual(core.normalize_url(self.SAMPLE),
                                 self.CLEAN_SHARE)
                with _fake_network(core, self.SAMPLE, self.HTML):
                    resolved = core.normalize_phone_url(self.SAMPLE)
                    self.assertEqual(resolved, self.CANONICAL)
                    # New Venom determines the surface from the resolved
                    # permalink; an opaque share/v token may also be live.
                    core.set_current_post_url(resolved)
                    self.assertTrue(core._is_reel_context())
                    targets = core.normalize_targets_for_phones(
                        [{"url": self.SAMPLE, "comment": "example"}])
                    self.assertEqual(targets[0]["url"], self.CANONICAL)
                    variants = core._fb_uri_variants(self.SAMPLE)
                    self.assertEqual(variants[:2],
                                     [self.CANONICAL, "fb://reel/42"])

    def test_unresolved_share_v_keeps_a_clean_phone_fallback(self):
        for core in (VENOM, POISON):
            with self.subTest(core=core.__name__):
                with _fake_network(core, self.SAMPLE,
                                   "<html>no canonical destination</html>"):
                    self.assertEqual(core.normalize_phone_url(self.SAMPLE),
                                     self.CLEAN_SHARE)
                    self.assertEqual(
                        core._fb_uri_variants(self.SAMPLE)[0], self.CLEAN_SHARE)


if __name__ == "__main__":
    unittest.main()
