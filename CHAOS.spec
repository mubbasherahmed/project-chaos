# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the CHAOS Control Panel (CHAOS.exe).
# Build:  py -3.11 -m PyInstaller CHAOS.spec --noconfirm
# Output: dist\CHAOS\CHAOS.exe  (one folder; copy the whole folder to install)
import glob
import os
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

# Bundle the Whisper "base" speech model so the app works offline on any PC
hub = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub", "models--Systran--faster-whisper-base", "snapshots")
snapshots = sorted(glob.glob(os.path.join(hub, "*")))
if not snapshots:
    raise SystemExit("Whisper base model not cached. Run: py -3.11 test_mic.py --download")
whisper_dir = snapshots[-1]
# The HF cache keeps files as hash-named blobs behind links; copy them out under their real names
import shutil
staging = os.path.join("build", "whisper-base")
os.makedirs(staging, exist_ok=True)
for f in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
    shutil.copyfile(os.path.realpath(os.path.join(whisper_dir, f)), os.path.join(staging, f))

datas = [("true_sight_ocr.ps1", "."), ("chaos_new.ico", "."), (staging, "models/faster-whisper-base")]
datas += collect_data_files("customtkinter")
datas += collect_data_files("faster_whisper")      # Silero VAD model
binaries = collect_dynamic_libs("ctranslate2")
hiddenimports = (["pyttsx3.drivers", "pyttsx3.drivers.sapi5", "comtypes.stream", "pycaw.pycaw", "psutil"]
                 + collect_submodules("comtypes.gen"))

# Large packages installed on the dev PC that CHAOS never imports
excludes = ["torch", "torchvision", "torchaudio", "facenet_pytorch", "tensorflow", "keras", "jax",
            "matplotlib", "pandas", "scipy", "sklearn", "IPython", "notebook", "jupyter",
            "PyQt5", "PyQt6", "PySide2", "PySide6", "transformers", "huggingface_hub.inference"]

a = Analysis(
    ["chaos_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CHAOS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["chaos_new.ico"],
    version=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CHAOS",
)
