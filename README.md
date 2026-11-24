# C.H.A.O.S. — Command Handling AI Operations Software

A privacy-first, fully offline voice assistant for Windows 10/11. CHAOS listens to spoken commands, controls the operating system, answers questions with a local AI model, reads the screen on demand ("True Sight"), and keeps its memory in a local database. No user data is sent over the internet.

BSCS Final Year Project 2026, SCIT Lahore Campus.
Team: Muhammad Afzaal Akhtar, Muhammad Abdul Rehman Khan, Mubbasher Ahmed. Supervisor: Dr. Umar.

## What runs where

| Part | Technology | Runs on |
| --- | --- | --- |
| Speech-to-text | faster-whisper (`base` model, ~145 MB) | This PC (CPU) |
| Voice output | Windows SAPI voices | This PC |
| AI answers | Ollama with Llama 3.2 3B | This PC (localhost:11434) |
| True Sight | Windows built-in OCR + the local model | This PC |
| Memory | SQLite (`chaos_data.db`) | This PC |

`OFFLINE_MODE = True` (top of `CHAOS.py`) blocks every internet-only feature: weather, news, Wikipedia, web search, WhatsApp, speed test and public IP lookup.

## Measured on the development PC

- CHAOS process RAM: about 222 MB (limit 1 GB)
- Basic voice command: about 1.1 s from end of speech to reply (target 2 s)
- True Sight: 4.5 to 4.8 s per scan with Llama 3.2 3B (target 5 s)

## Setup (needs internet once)

1. Install Python 3.11, then:
   ```powershell
   py -3.11 -m pip install -r requirements.txt
   ```
2. Install [Ollama](https://ollama.com) and pull the model:
   ```powershell
   ollama pull llama3.2
   ```
3. Cache the speech model and test your microphone:
   ```powershell
   py -3.11 test_mic.py --download
   ```

After this, CHAOS works with the network disabled.

## Run

**Desktop app (CHAOS Control Panel):**

```powershell
py -3.11 chaos_app.py
```

The Control Panel has the 9 screens designed in Deliverable 4: Unlock (local PIN), Dashboard, Conversation, True Sight, Productivity, Memory and History, Settings, Performance, and error/empty-state banners, plus a System Check screen. On first run, System Check starts Ollama if it is installed and offers a one-click download of Llama 3.2.

**Console version:**

```powershell
.\RUN_CHAOS.bat
```

See `CHAOS_MANUAL.txt` for every voice command.

## Build CHAOS.exe

```powershell
py -3.11 -m pip install pyinstaller
py -3.11 -m PyInstaller CHAOS.spec --noconfirm
```

The result is `dist\CHAOS\CHAOS.exe`. Python is bundled, and so is the Whisper speech model, so the target PC needs only Windows 10/11 and [Ollama](https://ollama.com). Copy the whole `dist\CHAOS` folder. In the packaged app, user data is stored in `%LOCALAPPDATA%\CHAOS`.

## Files

| File | Purpose |
| --- | --- |
| `CHAOS.py` | The assistant engine (voice, OS control, AI, True Sight, database) |
| `chaos_app.py` | CHAOS Control Panel desktop app (GUI) |
| `CHAOS.spec` | PyInstaller build spec for CHAOS.exe |
| `launcher.py`, `CHAOS_Launcher.spec` | System-tray launcher and its PyInstaller build spec |
| `true_sight_ocr.ps1` | Windows OCR helper used by True Sight |
| `test_mic.py` | Microphone and speech-model check |
| `list_voices.py` | Lists the installed Windows voices |
| `CHAOS_MANUAL.txt` | User manual |

Runtime data (`chaos_data.db`, `workspace/`) is created locally and is excluded from the repository.
