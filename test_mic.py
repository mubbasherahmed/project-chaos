"""
CHAOS Microphone Test Script (offline)
Checks that your microphone works with CHAOS's local speech recognition (faster-whisper).
No audio leaves your PC.

    python test_mic.py              test the microphone with the cached Whisper model
    python test_mic.py --download   one-time download of the Whisper model (needs internet)
"""
import os
import sys

DOWNLOAD = "--download" in sys.argv
if not DOWNLOAD:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")  # never reach the internet during a normal test
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import numpy as np
import speech_recognition as sr
from faster_whisper import WhisperModel

MODEL_SIZE = "base"  # keep in sync with WHISPER_MODEL_SIZE in CHAOS.py

print("=" * 50)
print("  MICROPHONE TEST (offline)")
print("=" * 50)

print(f"\nLoading local speech model: faster-whisper '{MODEL_SIZE}'...")
try:
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
except Exception as e:
    print(f"\n✗ Could not load the model: {e}")
    print("  Run 'python test_mic.py --download' once while online to fetch it.")
    input("Press Enter to exit...")
    sys.exit(1)
print("Model ready.")
if DOWNLOAD:
    print("Model downloaded and cached. CHAOS can now recognise speech offline.")

# List all microphones
print("\nAvailable microphones:")
for i, mic in enumerate(sr.Microphone.list_microphone_names()):
    print(f"  [{i}] {mic}")

# Test default microphone
r = sr.Recognizer()
print("\n" + "-" * 50)
print("Testing default microphone...")
print("Speak something when you see 'Listening...'")
print("-" * 50)

try:
    with sr.Microphone() as source:
        print("\nAdjusting for ambient noise (2 seconds)...")
        r.adjust_for_ambient_noise(source, duration=2)
        print(f"Energy threshold set to: {r.energy_threshold}")

        print("\nListening... (speak now)")
        audio = r.listen(source, timeout=10, phrase_time_limit=10)
        print("Got audio! Processing locally...")

    raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    segments, info = model.transcribe(samples, beam_size=1, vad_filter=True)
    text = " ".join(s.text for s in segments).strip()
    if text:
        print(f"\n✓ SUCCESS! You said: '{text}'  (language: {info.language})")
    else:
        print("\n✗ Could not understand audio")
        print("  Try speaking louder or closer to the mic")

except sr.WaitTimeoutError:
    print("\n✗ No speech detected within 10 seconds")
    print("  Make sure your microphone is working")
except Exception as e:
    print(f"\n✗ Error: {e}")

print("\n" + "=" * 50)
input("Press Enter to exit...")
