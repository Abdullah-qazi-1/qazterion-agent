# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []

# Third-party packages that use dynamic imports / plugins
for pkg in ('litellm', 'openai', 'cryptography'):
    tmp_ret = collect_all(pkg)
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# Our own backend packages: PyInstaller's static analysis can miss modules
# that are imported dynamically (e.g. via importlib, plugin registries), so
# explicitly pull in every submodule of each first-party package.
for pkg in (
    'qz_core',
    'qz_pool',
    'qz_providers',
    'qz_recovery',
    'qz_router',
    'qz_sandbox',
    'qz_security',
    'qz_tasks',
    'qz_validation',
):
    hiddenimports += collect_submodules(pkg)

# Top-level single-file backend modules imported dynamically or lazily
hiddenimports += [
    'qz_agent',
    'qz_tools',
    'qz_desktop_backend',
    'qz_context',
    'qz_environment',
    'qz_health',
    'qz_indexer',
    'qz_keystore',
    'qz_memory',
    'qz_proxy_manager',
    'qz_repair',
    'qz_telemetry',
    'qz_task_compiler',
    'qz_usage_tracker',
    'generate_config',
]

# Non-python files the backend reads at runtime
datas += [
    ('config.yaml', '.'),
]

a = Analysis(
    ['qz_desktop_bridge.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    # The backend is a long-lived JSON-RPC stdio server.  A windowed
    # executable can lose usable stdin/stdout handles on Windows, making a
    # healthy bundled backend appear unavailable. Electron launches this with
    # windowsHide=True, so the console subsystem keeps reliable pipes without
    # showing a console window to installed-app users.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
