# -*- mode: python ; coding: utf-8 -*-
"""Build unmodified upstream FBVenom with the hub's external runtime hook."""
from pathlib import Path
import sys

project_root = Path(SPECPATH).resolve()
vendor_root = project_root / 'FBVenom'
upstream_spec = vendor_root / 'FBVenom.spec'
runtime_hook = project_root / 'integration' / 'venom_runtime_hook.py'
real_analysis = Analysis


def managed_analysis(*args, **kwargs):
    # Keep all upstream dependencies, assets and packaging settings intact.
    kwargs['pathex'] = [str(vendor_root), str(project_root), *kwargs.get('pathex', [])]
    kwargs['runtime_hooks'] = [str(runtime_hook), *kwargs.get('runtime_hooks', [])]
    kwargs['hiddenimports'] = [*kwargs.get('hiddenimports', []), 'venom_bridge']
    return real_analysis(*args, **kwargs)


sys.path[:0] = [str(vendor_root), str(project_root)]
upstream_namespace = dict(globals())
upstream_namespace.update(
    SPEC=str(upstream_spec), SPECPATH=str(vendor_root), Analysis=managed_analysis)
exec(compile(upstream_spec.read_text(encoding='utf-8'), str(upstream_spec), 'exec'), upstream_namespace)

# A vanilla upstream EXE is never given a managed-mode capability marker.
from venom_bridge import write_build_metadata
write_build_metadata(upstream_namespace['exe'].name)
