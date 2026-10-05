# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the desktop backend (JSON-RPC over stdio for Electron).
# Build: pip install -e .[build] && pyinstaller qz_backend.spec
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = []
# First-party packages: include every submodule (some are imported lazily).
for pkg in (
    'qz_cli',
    'qz_core',
    'qz_providers',
    'qz_recovery',
    'qz_sandbox',
    'qz_security',
    'qz_tasks',
    'qz_validation',
):
    hiddenimports += collect_submodules(pkg)

hiddenimports += [
    'qz_agent',
    'qz_context',
    'qz_desktop_backend',
    'qz_environment',
    'qz_health',
    'qz_indexer',
    'qz_keystore',
    'qz_memory',
    'qz_paths',
    'qz_repair',
    'qz_task_compiler',
    'qz_tools',
    'qz_usage_tracker',
]

# Non-Python files read at runtime.
datas = [
    ('qz_providers/default_providers.yaml', 'qz_providers'),
]

a = Analysis(
    ['qz_desktop_bridge.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Heavy optional packages that are never needed by the backend.
    excludes=['tkinter', 'sentence_transformers', 'torch', 'numpy', 'litellm'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='qz_backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Long-lived JSON-RPC stdio server: the console subsystem keeps reliable
    # stdin/stdout pipes; Electron launches it with windowsHide=True.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
