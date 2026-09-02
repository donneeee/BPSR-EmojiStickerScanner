# BPSR Emoicon Scanner

Exports Blue Protocol: Star Resonance emoji and sticker assets to `emoicons.zip`.

This repository contains the scanner source and build files. It does not include
extracted game assets.

## Basic use

Double-click `BPSR-Emoicon-Scanner.exe`.

The scanner opens a small window where you can choose:

- the BPSR game location
- the output folder for `emoicons.zip`

Then press **Export emoicons.zip**.

The game location can be the game root, the `container` folder, or `m0.pkg`
itself.

Common examples:

```text
C:\Program Files (x86)\Steam\steamapps\common\Blue Protocol Star Resonance
G:\SteamLibrary\steamapps\common\Blue Protocol Star Resonance\bpsr
G:\SteamLibrary\steamapps\common\Blue Protocol Star Resonance\bpsr\BPSR_STEAM_Data\StreamingAssets\container\m0.pkg
```

The selected output folder receives `emoicons.zip`.

## Choose a game folder

```powershell
.\BPSR-Emoicon-Scanner.exe --gui
```

The GUI also has an **Auto Detect** button when the Steam install is in a common
location.

## Command-line use

```powershell
.\BPSR-Emoicon-Scanner.exe --cli --game "G:\SteamLibrary\steamapps\common\Blue Protocol Star Resonance\bpsr" --out "C:\Users\You\Desktop"
```

The ZIP contains decoded PNGs plus manifest, code, summary, and contact-sheet files.

Add `--keep-loose` if you also want the unpacked `bpsr_emoicon_export/`
inspection folder beside the zip.

## Build from source

```powershell
python -m pip install -r requirements.txt
python -m PyInstaller --clean --noconfirm BPSR-Emoicon-Scanner.spec
```

The built executable will be written to `dist\BPSR-Emoicon-Scanner.exe`.

GitHub Actions also builds a Windows artifact on pushes to `main` and manual
workflow runs.
