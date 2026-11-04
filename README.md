# footage_logger

Turns long gameplay captures into searchable, timestamped logs. For each video it:

1. grabs frames at scene changes (and at least every few seconds),
2. transcribes the audio with Whisper,
3. asks a local vision model (via Ollama) to describe each frame,
4. writes a Markdown log with thumbnails, a JSON log for searching, and a Final Cut Pro XML file that imports the clip with a marker for every logged moment.

Everything runs locally, so no footage leaves your machine. Jobs can be resumed: stop it at any time and run the same command again to continue where it left off.

It runs on macOS and Windows.

## Requirements

- Python 3.9 or newer (the script only uses the standard library, plus a Whisper package for transcription)
- ffmpeg 5.1 or newer (provides `ffmpeg` and `ffprobe`)
- [Ollama](https://ollama.com) with a vision model
- A Whisper library: `mlx-whisper` on Apple Silicon Macs, or `faster-whisper` on Windows and other machines

## Installing on macOS

### 1. ffmpeg

```sh
brew install ffmpeg
```

### 2. Ollama and the vision model

Install Ollama from <https://ollama.com/download> or with Homebrew:

```sh
brew install ollama
```

Start it, either by opening the Ollama app or by running:

```sh
ollama serve
```

Then pull the default vision model:

```sh
ollama pull gemma4:12b
```

### 3. Whisper (speech transcription)

On an Apple Silicon Mac (recommended, because it runs on the GPU):

```sh
pip3 install mlx-whisper
```

On an Intel Mac:

```sh
pip3 install faster-whisper
```

To keep these packages separate from your system Python, you can install them in a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install mlx-whisper   # or faster-whisper
```

## Installing on Windows

Run these commands in PowerShell or Windows Terminal. They use `winget`, which comes with Windows 10 and 11. You can also download each installer from the linked websites.

### 1. Python

Install Python from <https://www.python.org/downloads/> (tick **Add python.exe to PATH** in the installer), or with:

```powershell
winget install Python.Python.3.12
```

On Windows, run Python with `py` rather than `python3`.

### 2. ffmpeg

```powershell
winget install Gyan.FFmpeg
```

Close and reopen the terminal afterwards so it picks up the new `PATH`, then run `ffmpeg -version` to check that it works.

### 3. Ollama and the vision model

Install Ollama from <https://ollama.com/download>, or with:

```powershell
winget install Ollama.Ollama
```

On Windows, Ollama runs in the background after installation (look for the llama icon in the system tray). If it isn't running, start **Ollama** from the Start menu.

Then pull the default vision model:

```powershell
ollama pull gemma4:12b
```

### 4. Whisper (speech transcription)

`mlx-whisper` only works on Apple Silicon Macs, so use `faster-whisper` on Windows:

```powershell
py -m pip install faster-whisper
```

If you have an NVIDIA graphics card, `faster-whisper` uses it automatically, which makes transcription much faster. To do that it needs NVIDIA's CUDA 12 cuBLAS and cuDNN 9 libraries; the [faster-whisper README](https://github.com/SYSTRAN/faster-whisper#gpu) explains how to install them. Without a supported GPU, transcription runs on the CPU, which works but is slower. If you don't want to install the libraries, pass `--device cpu` to always use the CPU.

To keep these packages separate from your system Python, you can install them in a virtual environment:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install faster-whisper
```

## Notes for all platforms

- You can use any Ollama model that accepts images by passing `--model <name>`. If you do, pull that model first.
- The script uses `mlx-whisper` if it is installed and falls back to `faster-whisper` if it is not.
- The Whisper model (`whisper-large-v3-turbo` by default) downloads from Hugging Face the first time you run the script, so that first run needs an internet connection and takes longer.
- If you don't need transcripts, you can skip installing Whisper and run with `--no-audio`.

## Usage

The examples below use the macOS command, `python3`. On Windows, replace `python3` with `py`, for example:

```powershell
py footage_logger.py log "D:\Footage\Pragmata captures" --context "Game: Pragmata"
```

Put quotes around any path that contains spaces.

### Log footage

Pass one or more video files or folders. Folders are searched recursively.

```sh
python3 footage_logger.py log "/Volumes/Footage/Pragmata captures" --context "Game: Pragmata"
```

`--context` is optional but recommended. It gives the vision model background on the footage, which helps it describe frames accurately.

By default the logs go into a `logs` folder inside the first folder you pass. To choose a different location, use `--out`.

Videos that already have a finished log are skipped. Use `--force` to redo them.

#### Common options

| Option | Default | What it does |
| --- | --- | --- |
| `--out DIR` | `<folder>/logs` | Where logs are written |
| `--context TEXT` | | Background on the footage for the vision model |
| `--model NAME` | `gemma4:12b` | Ollama vision model to use |
| `--ollama-url URL` | `http://localhost:11434` | Address of the Ollama server |
| `--max-gap SEC` | `8` | Always grab a frame at least this often |
| `--min-gap SEC` | `2` | Never grab scene-change frames closer together than this |
| `--scene 0-1` | `0.3` | Scene-change sensitivity (lower values grab more frames) |
| `--frame-width PX` | `1280` | Width of the frames sent to the model |
| `--audio-track N` | `0` | Which audio track to transcribe, counting from 0 |
| `--no-audio` | | Skip transcription |
| `--whisper-model NAME` | | Use a different Whisper model |
| `--language CODE` | auto-detect | Force the spoken language, e.g. `en` |
| `--device auto\|cpu\|cuda` | `auto` | Where `faster-whisper` runs: `auto` uses an NVIDIA GPU if one is found, `cpu` always uses the CPU, `cuda` requires an NVIDIA GPU. `mlx-whisper` ignores this and always uses the Mac's GPU |
| `--no-thumbnails` | | Leave frame images out of the Markdown log |
| `--no-fcpxml` | | Don't write the Final Cut Pro marker file |
| `--timeout SEC` | `300` | How long to wait for the model on each frame |
| `--force` | | Redo videos that already have a log |

Run `python3 footage_logger.py log -h` (or `py footage_logger.py log -h` on Windows) to see the full list.

### Search logs

```sh
python3 footage_logger.py search "/Volumes/Footage/Pragmata captures" "hugh" "lab"
```

Results must contain all of the search terms, which are matched against frame descriptions, on-screen text, tags and speech. You can pass the footage folder or its `logs` folder. If you used `--out`, pass that folder instead.

| Option | What it does |
| --- | --- |
| `--type TYPE` | Only show one kind of moment: `gameplay`, `cutscene`, `dialogue`, `menu`, `map`, `loading`, `title` or `other` |
| `--limit N` | Maximum number of results to show (default 50) |

## Output

For each video, the `logs` folder contains:

- `<name>.md`: a readable, timestamped log with thumbnails, descriptions, on-screen text and speech
- `<name>.fcpxml`: import this into Final Cut Pro (File → Import → XML) to get the clip with a marker for every moment
- `<name>/`: working files, including the extracted frames, the transcript, the frame descriptions and `log.json`, which the `search` command reads

Don't delete the `<name>/` folder. The Markdown thumbnails and the search command both depend on it.

Final Cut Pro is only available on macOS. On Windows you can add `--no-fcpxml` to skip this file; the Markdown and JSON logs and the `search` command work the same on both platforms.

## Troubleshooting

- **"can't reach Ollama"**: open the Ollama app (on Windows, start it from the Start menu), or run `ollama serve`.
- **"Ollama doesn't have the model"**: run `ollama pull <model>`.
- **"no Whisper library found"**: install a Whisper library (see the install steps above), or run with `--no-audio`. This message always suggests `mlx-whisper`, but on Windows you need `faster-whisper` instead.
- **"ffmpeg not found"**: install ffmpeg (`brew install ffmpeg` on macOS, `winget install Gyan.FFmpeg` on Windows). On Windows, reopen the terminal after installing.
- **ffmpeg reports an unknown `-fps_mode` option**: your ffmpeg is older than 5.1, so upgrade it.
- **Windows: "Library cublas64_12.dll is not found" (or a cuDNN DLL is missing)**: `faster-whisper` found an NVIDIA GPU but not the CUDA libraries it needs. Either install them (see step 4 of the Windows instructions), or run transcription on the CPU instead:
  ```powershell
  py footage_logger.py log "D:\Footage\Pragmata captures" --device cpu
  ```
- **Windows: `UnicodeEncodeError` when saving the script's output to a file**: run `$env:PYTHONUTF8 = "1"` first, then run the command again.
