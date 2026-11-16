# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

project_root = Path(SPECPATH)
branding = project_root / 'assets' / 'branding'

# Child apps stay external processes. Ship their separately built executables
# alongside the hub under FBPoison/ and FBVenom/; do not bundle private settings.
a = Analysis(
    [str(project_root / 'fbvenoison.py')],
    pathex=[str(project_root)],
    binaries=[],
    datas=[(str(branding), 'assets/branding')],
    hiddenimports=['hub', 'boxes', 'login_panel', 'brand_ui', 'updater', 'version', 'demo'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='fbvenoison',
    icon=str(branding / 'venoison.ico'),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
