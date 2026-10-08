# PS3ToPC

**PS3 Legacy Console Edition → Minecraft Java Edition**

PS3ToPC converts PlayStation 3 Legacy Console Edition Minecraft saves (`GAMEDATA` / McRegion) into Java Edition Anvil worlds.

The repository keeps the conversion engine small and dependency-light. The desktop application adds a polished, native-looking dark interface: choose the PS3 save folder, choose where the converted world should be created, then press **CONVERTI MONDO**. The program automatically searches the selected folder for `GAMEDATA`.

## Features

- Automatic `GAMEDATA` discovery inside the selected save folder.
- Native folder selection dialogs on Windows, macOS and Linux.
- Clean rounded-card UI with no third-party GUI toolkit.
- Platform-aware system fonts for Windows, macOS and Linux.
- Conversion progress with region count and percentage.
- Creates a fresh Java-world folder, avoiding accidental overwrites.
- Handles Overworld, Nether and End regions.
- Preserves the conversion fixes already present in the engine for special blocks such as beds and ender chests.
- No user save/world is included in the repository.
- No mandatory third-party Python packages.

## Requirements

Python **3.10+**.

Tkinter is included with standard Python installations on Windows and macOS. On some Linux distributions it must be installed separately (commonly `python3-tk`).

An optional NumPy accelerator can be installed with:

```bash
python -m pip install -r requirements-optional.txt
```

The converter still works without NumPy.

## Start the application

```bash
python app.py
```

On Linux you can also run `./start_linux.sh`; on macOS you can open `start_macos.command`.

On Windows you can also run:

```text
start_windows.bat
```

The application does not need a web browser or local web server: it opens as a normal desktop window on Windows, macOS and Linux.

## Command line

The conversion engine is also usable directly:

```bash
python core/ps3_converter.py "PATH/TO/GAMEDATA" "PATH/TO/OUTPUT"
```

The GUI is intentionally only a frontend. The actual PS3 → Java conversion remains in `core/ps3_converter.py`.

## Repository layout

```text
PS3ToPC/
├─ app.py
├─ core/
│  ├─ ps3_converter.py
│  ├─ convert_region_worker.py
│  ├─ legacy_nbt_writer.py
│  ├─ nbt_tools.py
│  └─ repair_special_blocks.py
├─ requirements.txt
├─ requirements-optional.txt
└─ .github/workflows/
```

## Building a standalone desktop executable

The repository includes a GitHub Actions workflow for building a platform package with PyInstaller. Releases can be produced for Windows, macOS and Linux without asking end users to install Python themselves.

For local development:

```bash
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed --name PS3ToPC app.py
```

The resulting executable is placed in `dist/`. The GitHub Actions workflow also builds Windows, macOS and Linux artifacts automatically when a `v*` tag is pushed.

## Important

A PS3 save is represented by its original `GAMEDATA` file. Do not edit the contents of `core/` during a conversion. The GUI passes the discovered `GAMEDATA` to the tested conversion engine exactly as-is.

This project is independent software and is not affiliated with Sony Interactive Entertainment or Mojang/Microsoft.
