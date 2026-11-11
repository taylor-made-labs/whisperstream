# Build from source

You'll need Python 3.12 to 3.14 with [uv](https://docs.astral.sh/uv/), Node.js
22.13+, and Rust.

- **Windows 10 or 11 (x64):** also install the Rust MSVC toolchain and Visual
  Studio C++ Build Tools with the **Desktop development with C++** workload.
- **macOS (beta):** macOS 14 or later and the Xcode Command Line Tools.
  Use native Python and Node for your processor. Apple Silicon supports Python
  3.12 to 3.14; Intel source builds support Python 3.12 or 3.13.

From the repository root on Windows:

```powershell
uv sync --locked --extra dev
uv run --locked python scripts/check-sherpa-source.py
powershell -NoProfile -File scripts/run_dev.ps1
```

On macOS:

```sh
uv sync --locked --extra dev
uv run --locked python scripts/check-sherpa-source.py
bash scripts/run_dev.sh
```

The launcher installs the frontend, stages supported native engines, starts the
backend and opens the app. Keep its terminal open while you use the app. It
stores data in a separate development folder, so it won't touch an installed
copy. Speech models download during first-run setup.

## Intel Mac source builds

Intel source builds use the CPU speech backend for dictation and Agent Speak.
The same hotkeys, agent session routing and local speech output are available;
no Apple Silicon emulation or cloud transcription is required. Transcription
speed depends on the model and CPU.

Use native x86_64 Python 3.12 or 3.13 and x64 Node. The launcher selects the
matching native frontend bindings and Rust sidecar names. Intel builds do not
start the Apple Silicon FluidAudio worker. The packaged Apple Silicon DMG and
`scripts/build_macos_internal.py` remain Apple Silicon only.

Managed local AI cleanup requires the Apple Silicon Metal runtime and is
unavailable in the Intel source build. Leave AI enhancement off, or explicitly
configure an external cleanup provider. Dictation and Agent Speak do not
require cleanup. The launcher stages fail-fast placeholders for unavailable
sidecars solely because Tauri validates all external binary names during a
source build; these placeholders do not provide those features.
