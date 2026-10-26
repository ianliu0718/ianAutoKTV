# PyInstaller build specification for OpenKTV-AI
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_dir = Path(SPECPATH)
datas = []
binaries = []
scipy_special = project_dir / ".venv" / "Lib" / "site-packages" / "scipy" / "special"
cython_special = next(scipy_special.glob("cython_special*.pyd"), None)
if cython_special:
    datas.append((str(cython_special), "scipy/special"))

datas += collect_data_files("spleeter")
datas.append((str(project_dir / ".venv" / "Lib" / "site-packages" / "setuptools" / "_vendor" / "jaraco" / "text" / "Lorem ipsum.txt"), "setuptools/_vendor/jaraco/text"))
hiddenimports = collect_submodules("spleeter")
hiddenimports += ["engineio.async_drivers.threading", "scipy.special.cython_special"]

a = Analysis(
    [str(project_dir / "main.py")],
    pathex=[str(project_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hooksconfig={},
    runtime_hooks=[str(project_dir / "pyinstaller_runtime_hook.py")],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    name="ianAutoKTV_Server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(project_dir / "ianAutoKTV.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ianAutoKTV_Server",
)
