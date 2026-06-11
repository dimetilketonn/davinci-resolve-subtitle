# DaVinci Resolve Auto-Subtitle (Whisper + VAD)

Generate accurately-timed subtitles for your DaVinci Resolve timelines with **one click**, fully offline, using [faster-whisper](https://github.com/SYSTRAN/faster-whisper).

The script renders your timeline's audio, transcribes it locally on your GPU (or CPU), fixes Whisper's early-timestamp drift using Silero VAD, and imports the resulting SRT straight into your timeline's subtitle track. Built and tested on the **free version** of DaVinci Resolve — no Studio license required.

## Features

- **One-click workflow inside Resolve**: `Workspace > Scripts > generate_subtitle` does everything — render, transcribe, import, place on subtitle track.
- **Two subtitle modes, auto-detected from timeline resolution**:
  - **Shorts mode** (vertical timelines, e.g. 1080x1920): word-level chunks (max 4 words, max 1.5 s) for punchy short-form captions.
  - **Normal mode** (horizontal timelines): Netflix-style segmentation (max 42 chars/line, 2 lines, 17 CPS, smart breaks at punctuation and pauses).
- **VAD timestamp correction**: Whisper tends to start segments 0.2–0.7 s before the actual speech. A Silero VAD pass detects real speech onsets and snaps segment (and word) timestamps to them.
- **Optional English translation** of the subtitles via DeepL or Claude API.
- Works with Turkish out of the box (`--language tr` default) but supports any Whisper language.

## Repository contents

| File | Purpose |
|------|---------|
| `generate_subtitle.py` | The DaVinci Resolve script (goes into Resolve's Scripts folder) |
| `transcribe.py` | Standalone transcription engine (also usable from the command line) |

---

## 1. Requirements

- **Windows 10/11** (paths below are for Windows; the scripts also work on macOS/Linux with adjusted paths)
- **DaVinci Resolve 18+** (free or Studio)
- **Python 3.9–3.12** installed on the system (Miniconda/Anaconda works great). Resolve's console must show an active `Py3` tab — if it doesn't, install Python and restart Resolve.
- **For GPU transcription (recommended)**: an NVIDIA GPU with ~6 GB+ VRAM (tested on RTX 3060) and recent drivers. No manual CUDA toolkit installation is needed — the CUDA libraries come from pip packages.
- **Disk space**: ~3 GB for the `large-v3` Whisper model (downloaded automatically on first run).

## 2. Install Python dependencies

Open a terminal (cmd) and run:

```bash
pip install faster-whisper
```

**GPU users** also need the NVIDIA runtime libraries (no CUDA toolkit download required):

```bash
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

> `transcribe.py` automatically locates these pip-installed DLLs on Windows and adds them to the PATH at startup, so you don't need to configure anything.

**Optional — translation support:**

```bash
pip install deepl        # for --translate deepl  (requires DEEPL_API_KEY env var)
pip install anthropic    # for --translate claude (requires ANTHROPIC_API_KEY env var)
```

## 3. Whisper model download

Nothing to do manually. On the first run, faster-whisper downloads the model you configured (default: `large-v3`, ~3 GB) from Hugging Face and caches it under `%USERPROFILE%\.cache\huggingface`. Subsequent runs load it from disk and work fully offline.

Smaller/faster alternatives if your hardware is limited: `medium`, `small`, `base` (pass `--model small` or change the default in `transcribe.py`).

## 4. GPU vs CPU configuration

The compute settings live in `transcribe.py` (CLI flags) and are chosen by VRAM:

| Hardware | Flags | Notes |
|----------|-------|-------|
| NVIDIA 12 GB+ VRAM | `--device cuda --compute-type float16` | Best quality |
| NVIDIA 6–8 GB VRAM | `--device cuda --compute-type int8_float16` | Default; quality loss is minimal, `large-v3` fits |
| No NVIDIA GPU | `--device cpu --compute-type int8` | Works, but expect minutes instead of seconds; consider `--model small` |

To change the defaults used by the Resolve integration, edit the `argparse` defaults at the bottom of `transcribe.py` (`--device`, `--compute-type`, `--model`).

## 5. Test the engine from the command line (recommended first step)

Before touching Resolve, verify the engine works:

```bash
python transcribe.py path\to\audio.mp3 --shorts
```

Expected output ends with lines like:

```
[+] 12 segments produced. Detected language: tr (1.00)
[*] VAD: 17 speech regions (first at 00:00:00,288)
[*] Segment corrected: 00:00:00,000 -> 00:00:00,288
[+] Turkish subtitles saved: audio_tr.srt
```

If you see the `[*] VAD:` line, the timestamp correction is active.

## 6. Install the Resolve script

DaVinci Resolve picks up Python scripts from a special **Scripts** folder. Note: this is *not* inside `C:\Program Files` — it lives in your user profile under a hidden `AppData` folder.

1. Open File Explorer and paste this into the **address bar**, then press Enter:

   ```
   %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
   ```

   (`%APPDATA%` expands to `C:\Users\<you>\AppData\Roaming`. If the `Scripts\Utility` folders don't exist, create them.)

   Alternative location for all users on the machine (also hidden, paste into the address bar):

   ```
   C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
   ```

2. Copy `generate_subtitle.py` into that `Utility` folder.

3. Open `generate_subtitle.py` in any text editor and adjust the **CONFIG** block at the top:

   ```python
   PYTHON_EXE     = r"C:\Users\<you>\miniconda3\python.exe"   # the Python that has faster-whisper installed
   TRANSCRIBE_PY  = r"C:\path\to\this\repo\transcribe.py"     # full path to transcribe.py
   WORK_DIR       = r"C:\path\to\this\repo\resolve_temp"      # scratch folder (created automatically)
   TRANSLATE      = "none"                                    # "none", "deepl" or "claude"
   MODE           = "auto"                                    # "auto", "shorts" or "normal"
   ```

   Tips:
   - To find `PYTHON_EXE`, run `where python` in cmd and use the path that belongs to the environment where you ran `pip install faster-whisper`.
   - `WORK_DIR` is where the temporary audio render and the generated SRT files land. The script creates it if missing.

4. **Restart DaVinci Resolve.** The script now appears under `Workspace > Scripts > generate_subtitle`.

## 7. Usage inside DaVinci Resolve

1. Open your project and make the timeline you want to subtitle the **active timeline**.
2. Click `Workspace > Scripts > generate_subtitle`.
3. Watch progress in `Workspace > Console` (select the **Py3** tab). The script will:
   1. Render the timeline's audio into `WORK_DIR` (audio-only if possible; falls back to your last Deliver preset otherwise — both work).
   2. Run `transcribe.py` on it. With `large-v3` on a mid-range GPU this takes roughly 1–2 minutes for a 1-minute video. **Resolve's UI will appear frozen during this step — that's normal**, the script runs synchronously.
   3. Apply VAD timestamp correction and write the SRT.
   4. Import the SRT into the Media Pool and append it to the timeline's subtitle track.
4. When the console prints `TAMAMLANDI ✔` (done), your subtitles are on the **ST1** subtitle track. Style them via the Inspector as usual.

If automatic placement isn't supported by your Resolve version, the SRT is still in the Media Pool: right-click it → **Insert Selected Subtitles to Timeline**.

### Mode selection

- `MODE = "auto"` (default): vertical timeline (height > width) → shorts mode; horizontal → normal mode. The chosen mode is printed to the console.
- Force a mode with `MODE = "shorts"` or `MODE = "normal"`.

### Standalone CLI usage (without Resolve)

```bash
python transcribe.py input.mp3                      # Netflix-style segmentation
python transcribe.py input.mp3 --shorts             # word-level chunks for Shorts
python transcribe.py input.mp3 --translate deepl    # also produce English SRT
python transcribe.py input.mp4 --language en        # other source languages
python transcribe.py input.mp3 --device cpu --compute-type int8 --model small   # CPU mode
```

Output files are written next to the input (or to `--output-dir`): `<name>_tr.srt` and optionally `<name>_en.srt`.

## 8. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Script doesn't appear in `Workspace > Scripts` | Wrong folder. Re-check the `Utility` path in step 6 and restart Resolve. |
| Console shows `Format setting (wav): FAILED` | Harmless. Resolve fell back to your last Deliver preset; the script locates whatever file was rendered (wav/mov/mp4) and Whisper reads all of them. |
| `Render job could not be created` | Open the Deliver page once, select the **Audio Only** preset, then re-run the script. |
| `transcribe.py failed` with CUDA/cuDNN errors | Run `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` in the same Python environment, or switch to CPU mode. |
| First run is very slow | The model (~3 GB) is downloading. Watch the console; later runs are fast. |
| Subtitles slightly out of sync | Make sure the console shows `[*] VAD:` lines (timestamp correction active). You can also tune `threshold` / `min_silence_duration_ms` in `detect_speech_regions()` in `transcribe.py`. |
| Resolve UI frozen during transcription | Expected — the script waits for transcription to finish. Watch progress in the Console. |
| Python tabs (Py2/Py3) missing in Console | Resolve can't find a Python installation. Install Python 3.x system-wide and restart Resolve. |

## 9. How the VAD correction works (technical note)

Whisper (including faster-whisper) systematically starts segments before the speech actually begins — typically 0.2–0.7 s early, worst at the start of the file and right after pauses. For word-level Shorts captions this is very noticeable.

After transcription, `transcribe.py` runs Silero VAD (bundled with faster-whisper, no extra install) with `speech_pad_ms=0` to find the true speech regions. Any segment that starts more than 120 ms before its real speech onset gets snapped to it, with its word timestamps rescaled proportionally. Safety rails: short speech tails at a segment boundary are ignored (`lead_guard=0.15`), and no segment is ever shifted by more than 1 second (`max_shift=1.0`).

## License

MIT — do whatever you want, attribution appreciated.
