from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import os
import queue
import re
import shutil
import sys
import textwrap
import threading
import time
import traceback
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


MAX_LINE_BYTES = 4096
CHUNK_SIZE = 16 * 1024 * 1024
HIT_TERMS = (b"emoji", b"sticker")
ADDRESS_RE = re.compile(r"^address:(.*?) ->>>> hash:(\d+) ->>>> bundleHash:(\d+)$")
SPRITE_RE = re.compile(r"^spriteAddress:(\d+)-->spriteName:(.*?)--->atlasAddress:(\d+)$")


@dataclass(frozen=True)
class MetaEntry:
    key: int
    type: int
    index: int
    offset: int
    length: int


def main() -> int:
    ensure_std_streams()
    args = parse_args()
    if args.gui or (not args.cli and not sys.argv[1:]):
        return run_gui(args)

    pause_at_end = args.pause or (not args.no_pause and not sys.argv[1:] and os.name == "nt")

    try:
        run(args)
        return 0
    except KeyboardInterrupt:
        print("\nCanceled.")
        return 130
    except Exception as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        return 1
    finally:
        if pause_at_end:
            try:
                input("\nPress Enter to exit...")
            except EOFError:
                pass


def ensure_std_streams() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf8")


def run(args: argparse.Namespace) -> None:
    started = time.time()
    game_arg = args.game
    if args.choose:
        picked = choose_game_folder()
        if picked:
            game_arg = picked

    try:
        m0_path = resolve_m0_package(game_arg or "auto")
    except FileNotFoundError as error:
        if game_arg or args.choose:
            raise
        print(str(error))
        picked = choose_game_folder()
        if not picked:
            raise
        m0_path = resolve_m0_package(picked)
    container_dir = m0_path.parent
    meta_entries = load_meta_entries(container_dir)
    if not meta_entries:
        raise RuntimeError(f"Could not read package entries from {container_dir / 'meta.pkg'}")

    output_root = Path(args.out).expanduser().resolve() if args.out else executable_dir()
    export_root = output_root / "bpsr_emoicon_export"
    zip_path = Path(args.zip).expanduser().resolve() if args.zip else output_root / "emoicons.zip"
    prepare_export_root(export_root)

    print("BPSR emoji/sticker scanner")
    print(f"Game package: {m0_path}")
    print(f"Working export folder: {export_root}")
    print(f"Zip target: {zip_path}")

    item_names_path = None
    if not args.no_item_names:
        item_names_path = Path(args.item_names).expanduser().resolve() if args.item_names else find_item_names_json()
        if item_names_path and item_names_path.is_file():
            print(f"Item-name source: {item_names_path}")
        else:
            item_names_path = None
            print("Item-name source: not found; item icon IDs/names will be blank.")

    candidates = scan_m0(m0_path, meta_entries)
    write_candidates(export_root, m0_path, candidates)
    print(f"Address candidates: {len(candidates['addresses'])}")
    print(f"Sprite candidates: {len(candidates['sprites'])}")

    item_index = load_item_index(item_names_path) if item_names_path else {}
    records = sorted(
        dedupe((normalize_record(row) for row in candidates["addresses"]), record_key),
        key=compare_records,
    )
    token_context = build_token_context(records)

    manifest_base: list[dict[str, Any]] = []
    plan_items: list[dict[str, Any]] = []
    missing: list[str] = []

    for record in records:
        classified = classify_record(record)
        ids = identify_record(record, classified, token_context, item_index)
        image_plan = build_image_plan(record, classified, export_root)
        manifest_record = {
            "asset": record["basename"],
            "assetAddress": record["address"],
            "category": classified["category"],
            "family": classified["family"],
            "locale": classified.get("locale", ""),
            "relatedChatAsset": ids["relatedChatAsset"],
            "chatToken": ids["chatToken"],
            "chatId": ids["chatId"],
            "idConfidence": ids["confidence"],
            "idReason": ids["reason"],
            "stickerSyntax": ids["stickerSyntax"],
            "emojiPicSyntax": ids["emojiPicSyntax"],
            "photoStickerId": ids["photoStickerId"],
            "furnitureStickerId": ids["furnitureStickerId"],
            "itemIconPath": ids["itemIconPath"],
            "primaryItemId": ids["primaryItem"]["Id"] if ids["primaryItem"] else None,
            "primaryItemName": ids["primaryItemName"],
            "itemIds": [item.get("Id") for item in ids["itemIds"]],
            "itemNames": ids["itemNames"],
            "decodeAttempted": bool(image_plan),
            "decodeSkippedReason": "" if image_plan else skip_decode_reason(classified),
            "pngFile": relative_path(image_plan["PngFile"], export_root) if image_plan else "",
            "bundleHash": record["bundleHash"],
            "addressHash": record["addressHash"],
            "packageIndex": record["packageIndex"],
            "packageFile": f"m{record['packageIndex']}.pkg" if record["packageIndex"] is not None else "",
            "packageOffset": record["packageOffset"],
            "packageLength": record["packageLength"],
        }
        manifest_base.append(manifest_record)

        if not image_plan:
            continue

        entry = meta_entries.get(record["bundleHash"])
        if entry is None:
            missing.append(f"{record['address']} (missing package entry for {record['bundleHash']})")
            continue

        plan_items.append(
            {
                **image_plan,
                "Address": record["address"],
                "BundleHash": record["bundleHash"],
                "AddressHash": record["addressHash"],
                "PackageIndex": record["packageIndex"],
                "PackageFile": f"m{record['packageIndex']}.pkg" if record["packageIndex"] is not None else "",
                "PackageOffset": record["packageOffset"],
                "PackageLength": record["packageLength"],
            }
        )

    plan_path = export_root / "all_emoji_sticker_export_plan.json"
    write_json(
        plan_path,
        {
            "GeneratedAt": now_iso(),
            "M0Path": str(m0_path),
            "ItemNamesPath": str(item_names_path) if item_names_path else "",
            "Items": plan_items,
            "Missing": missing,
        },
    )

    decoder_result = decode_images(container_dir, meta_entries, plan_items, dry_run=args.dry_run)
    write_json(export_root / "all_emoji_sticker_export_result.json", decoder_result)

    exported_by_asset_id = {item["AssetId"]: item for item in decoder_result["Exported"]}
    failed_by_asset_id = {item["AssetId"]: item for item in decoder_result["Failed"]}
    plan_by_address = {item["Address"]: item for item in plan_items}

    manifest_items: list[dict[str, Any]] = []
    for item in manifest_base:
        plan_item = plan_by_address.get(item["assetAddress"])
        exported = exported_by_asset_id.get(plan_item["AssetId"]) if plan_item else None
        failed = failed_by_asset_id.get(plan_item["AssetId"]) if plan_item else None
        manifest_items.append(
            {
                **item,
                "pngExported": bool(exported),
                "pngWidth": exported.get("PngWidth") if exported else None,
                "pngHeight": exported.get("PngHeight") if exported else None,
                "pixelSha256": exported.get("PixelSha256", "") if exported else "",
                "selectedObjectName": exported.get("SelectedObjectName", "") if exported else "",
                "selectedObjectType": exported.get("SelectedObjectType", "") if exported else "",
                "pngError": failed.get("Error", "") if failed else "",
            }
        )

    summary = build_summary(m0_path, item_names_path, records, plan_items, manifest_items, missing, started)
    write_json(
        export_root / "all_emoji_sticker_manifest.json",
        {
            "GeneratedAt": now_iso(),
            "M0Path": str(m0_path),
            "ItemNamesPath": str(item_names_path) if item_names_path else "",
            "Summary": summary,
            "Items": manifest_items,
        },
    )
    write_json(export_root / "all_emoji_sticker_summary.json", summary)
    write_csv(export_root / "all_emoji_sticker_manifest.csv", manifest_items)
    write_code_files(export_root / "codes", manifest_items)
    write_readme(export_root / "README.md", summary)

    if not args.no_contact_sheets:
        build_contact_sheets(export_root, manifest_items)

    write_zip(export_root, zip_path)

    print("")
    print(f"PNG assets exported: {summary['PngExportedCount']}")
    print(f"PNG export failures: {summary['PngFailureCount']}")
    print(f"Unique chat tokens: {summary['ChatTokenCount']}")
    print(f"Wrote {zip_path}")
    if args.keep_loose:
        print(f"Wrote loose export files to {export_root}")
    else:
        shutil.rmtree(export_root, ignore_errors=True)


class QueueWriter:
    def __init__(self, messages: "queue.Queue[tuple[str, Any]]") -> None:
        self.messages = messages

    def write(self, text: str) -> int:
        if text:
            self.messages.put(("log", text))
        return len(text)

    def flush(self) -> None:
        pass


def run_gui(args: argparse.Namespace) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as error:
        print(f"GUI is unavailable: {error}", file=sys.stderr)
        return 1

    root = tk.Tk()
    root.title("BPSR Emoicon Scanner")
    root.geometry("760x560")
    root.minsize(680, 500)

    messages: "queue.Queue[tuple[str, Any]]" = queue.Queue()
    worker: dict[str, threading.Thread | None] = {"thread": None}
    current_output_dir: dict[str, Path] = {"path": executable_dir()}

    initial_game = args.game if args.game and args.game.lower() not in {"auto", "steam"} else ""
    initial_status = "Choose the BPSR game location and output folder, then export."
    if not initial_game:
        try:
            initial_game = str(resolve_m0_package("auto"))
            initial_status = "Auto-detected m0.pkg. You can change it if needed."
        except FileNotFoundError:
            initial_status = "Choose the BPSR game location and output folder, then export."

    game_var = tk.StringVar(value=initial_game)
    output_var = tk.StringVar(value=args.out or str(executable_dir()))
    contact_sheets_var = tk.BooleanVar(value=not args.no_contact_sheets)
    keep_loose_var = tk.BooleanVar(value=args.keep_loose)
    item_names_var = tk.BooleanVar(value=not args.no_item_names)
    status_var = tk.StringVar(value=initial_status)

    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    outer = ttk.Frame(root, padding=18)
    outer.grid(row=0, column=0, sticky="nsew")
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(5, weight=1)

    title = ttk.Label(outer, text="BPSR Emoicon Scanner", font=("Segoe UI", 16, "bold"))
    title.grid(row=0, column=0, sticky="w")
    subtitle = ttk.Label(
        outer,
        text="Exports every emoji and sticker it finds into emoicons.zip.",
        foreground="#555555",
    )
    subtitle.grid(row=1, column=0, sticky="w", pady=(2, 14))

    path_frame = ttk.LabelFrame(outer, text="Paths", padding=12)
    path_frame.grid(row=2, column=0, sticky="ew")
    path_frame.columnconfigure(1, weight=1)

    ttk.Label(path_frame, text="Game location").grid(row=0, column=0, sticky="w", padx=(0, 10))
    game_entry = ttk.Entry(path_frame, textvariable=game_var)
    game_entry.grid(row=0, column=1, sticky="ew")
    ttk.Button(path_frame, text="Browse Folder", command=lambda: browse_game_folder()).grid(row=0, column=2, padx=(8, 0))
    ttk.Button(path_frame, text="Pick m0.pkg", command=lambda: browse_m0_file()).grid(row=0, column=3, padx=(8, 0))
    ttk.Button(path_frame, text="Auto Detect", command=lambda: auto_detect_game()).grid(row=0, column=4, padx=(8, 0))

    example = (
        r"Example: C:\Program Files (x86)\Steam\steamapps\common\Blue Protocol Star Resonance"
        r"  or  G:\SteamLibrary\steamapps\common\Blue Protocol Star Resonance\bpsr"
    )
    ttk.Label(path_frame, text=example, foreground="#666666", wraplength=680).grid(
        row=1,
        column=1,
        columnspan=4,
        sticky="w",
        pady=(5, 12),
    )

    ttk.Label(path_frame, text="Output folder").grid(row=2, column=0, sticky="w", padx=(0, 10))
    output_entry = ttk.Entry(path_frame, textvariable=output_var)
    output_entry.grid(row=2, column=1, columnspan=3, sticky="ew")
    ttk.Button(path_frame, text="Browse", command=lambda: browse_output_folder()).grid(row=2, column=4, padx=(8, 0))
    ttk.Label(path_frame, text="emoicons.zip will be written here.", foreground="#666666").grid(
        row=3,
        column=1,
        columnspan=4,
        sticky="w",
        pady=(5, 0),
    )

    options_frame = ttk.Frame(outer)
    options_frame.grid(row=3, column=0, sticky="ew", pady=(14, 8))
    ttk.Checkbutton(options_frame, text="Generate contact sheets", variable=contact_sheets_var).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(options_frame, text="Use item names if found", variable=item_names_var).grid(row=0, column=1, sticky="w", padx=(18, 0))
    ttk.Checkbutton(options_frame, text="Keep unpacked inspection folder", variable=keep_loose_var).grid(row=0, column=2, sticky="w", padx=(18, 0))

    actions = ttk.Frame(outer)
    actions.grid(row=4, column=0, sticky="ew", pady=(4, 12))
    actions.columnconfigure(2, weight=1)
    export_button = ttk.Button(actions, text="Export emoicons.zip", command=lambda: start_export())
    export_button.grid(row=0, column=0, sticky="w")
    open_button = ttk.Button(actions, text="Open Output Folder", command=lambda: open_output_folder(), state="disabled")
    open_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
    progress = ttk.Progressbar(actions, mode="indeterminate")
    progress.grid(row=0, column=2, sticky="ew", padx=(14, 0))

    log_frame = ttk.LabelFrame(outer, text="Progress", padding=8)
    log_frame.grid(row=5, column=0, sticky="nsew")
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)
    log_text = tk.Text(log_frame, height=14, wrap="word", state="disabled")
    log_text.grid(row=0, column=0, sticky="nsew")
    scroll = ttk.Scrollbar(log_frame, command=log_text.yview)
    scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=scroll.set)

    status = ttk.Label(outer, textvariable=status_var, foreground="#333333")
    status.grid(row=6, column=0, sticky="ew", pady=(10, 0))

    def append_log(text: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.configure(state="disabled")

    def set_running(is_running: bool) -> None:
        state = "disabled" if is_running else "normal"
        export_button.configure(state=state)
        game_entry.configure(state=state)
        output_entry.configure(state=state)
        for child in path_frame.winfo_children() + options_frame.winfo_children():
            if child not in {game_entry, output_entry}:
                try:
                    child.configure(state=state)
                except tk.TclError:
                    pass
        if is_running:
            progress.start(12)
            open_button.configure(state="disabled")
        else:
            progress.stop()
            open_button.configure(state="normal" if current_output_dir["path"].exists() else "disabled")

    def browse_game_folder() -> None:
        initial = game_var.get().strip()
        folder = filedialog.askdirectory(
            parent=root,
            title="Choose Blue Protocol Star Resonance folder",
            initialdir=initial if initial and Path(initial).is_dir() else str(Path.home()),
        )
        if folder:
            game_var.set(folder)

    def browse_m0_file() -> None:
        initial = game_var.get().strip()
        initial_path = Path(initial)
        if initial and initial_path.is_file():
            initial_dir = str(initial_path.parent)
        elif initial and initial_path.is_dir():
            initial_dir = initial
        else:
            initial_dir = str(Path.home())
        file_path = filedialog.askopenfilename(
            parent=root,
            title="Choose m0.pkg",
            initialdir=initial_dir,
            filetypes=[("BPSR package", "m0.pkg"), ("Package files", "*.pkg"), ("All files", "*.*")],
        )
        if file_path:
            game_var.set(file_path)

    def browse_output_folder() -> None:
        initial = output_var.get().strip()
        folder = filedialog.askdirectory(
            parent=root,
            title="Choose output folder for emoicons.zip",
            initialdir=initial if initial and Path(initial).is_dir() else str(Path.home()),
        )
        if folder:
            output_var.set(folder)

    def auto_detect_game() -> None:
        try:
            m0_path = resolve_m0_package("auto")
        except Exception as error:
            status_var.set("Auto-detect failed. Use Browse Folder or Pick m0.pkg.")
            messagebox.showerror("Auto-detect failed", str(error), parent=root)
            return
        game_var.set(str(m0_path))
        status_var.set("Auto-detected m0.pkg.")

    def open_output_folder() -> None:
        path = current_output_dir["path"]
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                messagebox.showinfo("Output folder", str(path), parent=root)
        except Exception as error:
            messagebox.showerror("Open output failed", str(error), parent=root)

    def make_run_args() -> argparse.Namespace:
        game_text = game_var.get().strip().strip('"') or "auto"
        output_text = output_var.get().strip().strip('"') or str(executable_dir())
        current_output_dir["path"] = Path(output_text).expanduser().resolve()
        return argparse.Namespace(
            game=game_text,
            choose=False,
            out=output_text,
            zip="",
            item_names=args.item_names,
            no_item_names=not item_names_var.get(),
            no_contact_sheets=not contact_sheets_var.get(),
            keep_loose=keep_loose_var.get(),
            dry_run=False,
            pause=False,
            no_pause=True,
            gui=False,
            cli=True,
        )

    def start_export() -> None:
        run_args = make_run_args()
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        status_var.set("Exporting. This can take a minute.")
        set_running(True)

        def worker_main() -> None:
            writer = QueueWriter(messages)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    run(run_args)
                except Exception as error:
                    traceback.print_exc()
                    messages.put(("done", False, str(error)))
                else:
                    messages.put(("done", True, "Export complete."))

        thread = threading.Thread(target=worker_main, daemon=True)
        worker["thread"] = thread
        thread.start()

    def poll_messages() -> None:
        try:
            while True:
                message = messages.get_nowait()
                if message[0] == "log":
                    append_log(str(message[1]))
                elif message[0] == "done":
                    ok = bool(message[1])
                    detail = str(message[2])
                    set_running(False)
                    status_var.set(detail)
                    if ok:
                        messagebox.showinfo("Export complete", f"emoicons.zip was written to:\n{current_output_dir['path']}", parent=root)
                    else:
                        messagebox.showerror("Export failed", detail, parent=root)
        except queue.Empty:
            pass
        root.after(100, poll_messages)

    def on_close() -> None:
        thread = worker["thread"]
        if thread and thread.is_alive():
            if not messagebox.askyesno("Export still running", "Close anyway? The export will stop.", parent=root):
                return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(100, poll_messages)
    root.mainloop()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Blue Protocol: Star Resonance game packages and export emoji/sticker PNGs to emoicons.zip.",
    )
    parser.add_argument("--gui", action="store_true", help="Open the graphical scanner window.")
    parser.add_argument("--cli", action="store_true", help="Run in command-line mode instead of opening the GUI.")
    parser.add_argument(
        "--game",
        default="",
        help="Game folder, container folder, m0.pkg path, or preset: auto/steam. Default: auto.",
    )
    parser.add_argument(
        "--choose",
        action="store_true",
        help="Open a folder picker before scanning. Falls back to console input if unavailable.",
    )
    parser.add_argument(
        "--out",
        default="",
        help="Folder that receives emoicons.zip. Default: beside the exe.",
    )
    parser.add_argument("--zip", default="", help="Exact output zip path. Default: <out>/emoicons.zip.")
    parser.add_argument("--item-names", default="", help="Optional itemnames.json path for item icon IDs/names.")
    parser.add_argument("--no-item-names", action="store_true", help="Skip itemnames.json auto-detection.")
    parser.add_argument("--no-contact-sheets", action="store_true", help="Skip contact sheet PNG generation.")
    parser.add_argument("--keep-loose", action="store_true", help="Keep the unpacked bpsr_emoicon_export inspection folder.")
    parser.add_argument("--dry-run", action="store_true", help="Decode metadata but do not write PNG files.")
    parser.add_argument("--pause", action="store_true", help="Pause before exit.")
    parser.add_argument("--no-pause", action="store_true", help="Do not pause before exit.")
    return parser.parse_args()


def executable_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def choose_game_folder() -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        value = filedialog.askdirectory(title="Choose Blue Protocol Star Resonance folder")
        root.destroy()
        return value or ""
    except Exception:
        print("Folder picker is unavailable.")
        try:
            return input("Paste the game folder, container folder, or m0.pkg path: ").strip().strip('"')
        except EOFError:
            return ""


def resolve_m0_package(game_path: str) -> Path:
    candidates: list[Path] = []
    game_text = str(game_path or "auto").strip()
    preset = game_text.lower() if game_text.lower() in {"auto", "steam"} else ""

    if preset:
        for root in auto_game_roots():
            add_game_root_candidates(candidates, root)
    else:
        add_game_root_candidates(candidates, Path(game_text).expanduser())

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        if resolved.is_file() and resolved.name.lower() == "m0.pkg":
            return resolved

    if not preset:
        raise FileNotFoundError(f"Could not find m0.pkg from {game_path!r}")
    raise FileNotFoundError("Could not auto-detect m0.pkg. Rerun with --choose or --game <path>.")


def auto_game_roots() -> list[Path]:
    roots: list[Path] = []
    for steam_root in steam_install_root_candidates():
        for library_root in steam_library_root_candidates(steam_root):
            roots.append(library_root / "steamapps" / "common" / "Blue Protocol Star Resonance")

    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        roots.append(Path(f"{drive}:\\SteamLibrary\\steamapps\\common\\Blue Protocol Star Resonance"))
        roots.append(Path(f"{drive}:\\Games\\SteamLibrary\\steamapps\\common\\Blue Protocol Star Resonance"))

    return unique_paths(roots)


def steam_install_root_candidates() -> list[Path]:
    roots = [
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("ProgramFiles", ""),
    ]
    return unique_paths(Path(root) / "Steam" for root in roots if root)


def steam_library_root_candidates(steam_root: Path) -> list[Path]:
    roots = [steam_root]
    library_folders = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = library_folders.read_text(encoding="utf8", errors="ignore")
        for line in text.splitlines():
            match = re.search(r'"path"\s+"([^"]+)"', line)
            if match:
                roots.append(Path(match.group(1).replace("\\\\", "\\")))
                continue
            legacy = re.search(r'"\d+"\s+"([^"]*[\\/:][^"]*)"', line)
            if legacy:
                roots.append(Path(legacy.group(1).replace("\\\\", "\\")))
    except OSError:
        pass
    return unique_paths(roots)


def add_game_root_candidates(candidates: list[Path], root: Path) -> None:
    candidates.extend(
        [
            root,
            root / "m0.pkg",
            root / "container" / "m0.pkg",
            root / "StreamingAssets" / "container" / "m0.pkg",
            root / "BPSR_STEAM_Data" / "StreamingAssets" / "container" / "m0.pkg",
            root / "bpsr" / "BPSR_STEAM_Data" / "StreamingAssets" / "container" / "m0.pkg",
        ]
    )


def unique_paths(values: Any) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for value in values:
        if not value:
            continue
        path = Path(value)
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def load_meta_entries(container_dir: Path) -> dict[int, MetaEntry]:
    meta_path = container_dir / "meta.pkg"
    data = meta_path.read_bytes()
    offset = 0

    def read_i32() -> int:
        nonlocal offset
        value = int.from_bytes(data[offset : offset + 4], "little", signed=True)
        offset += 4
        return value

    def read_u32() -> int:
        nonlocal offset
        value = int.from_bytes(data[offset : offset + 4], "little", signed=False)
        offset += 4
        return value

    def read_u16() -> int:
        nonlocal offset
        value = int.from_bytes(data[offset : offset + 2], "little", signed=False)
        offset += 2
        return value

    read_i32()
    read_i32()
    read_i32()
    offset += 8
    read_u32()
    header_count = read_u16()
    offset += 16 * header_count

    entries: dict[int, MetaEntry] = {}

    def read_entry_section(count: int) -> None:
        nonlocal offset
        for _ in range(count):
            key = read_u32()
            entry_type = data[offset]
            offset += 1
            index = read_u16()
            entry_offset = read_i32()
            length = read_i32()
            entries[key] = MetaEntry(key, entry_type, index, entry_offset, length)

    read_entry_section(read_i32())
    read_entry_section(read_i32())
    return entries


def scan_m0(m0_path: Path, meta_entries: dict[int, MetaEntry]) -> dict[str, Any]:
    addresses: list[dict[str, Any]] = []
    sprites: list[dict[str, Any]] = []

    def on_line(line: str) -> None:
        address_match = ADDRESS_RE.match(line)
        if address_match:
            address = normalize(address_match.group(1))
            if not has_hit(address):
                return
            bundle_hash = int(address_match.group(3)) & 0xFFFFFFFF
            entry = meta_entries.get(bundle_hash)
            addresses.append(
                {
                    "address": address,
                    "addressHash": int(address_match.group(2)) & 0xFFFFFFFF,
                    "bundleHash": bundle_hash,
                    "packageIndex": entry.index if entry else None,
                    "packageOffset": entry.offset if entry else None,
                    "packageLength": entry.length if entry else None,
                    "basename": posix_basename(address),
                }
            )
            return

        sprite_match = SPRITE_RE.match(line)
        if sprite_match:
            sprite_name = normalize(sprite_match.group(2))
            if not has_hit(sprite_name):
                return
            sprites.append(
                {
                    "spriteAddress": int(sprite_match.group(1)) & 0xFFFFFFFF,
                    "spriteName": sprite_name,
                    "atlasAddress": int(sprite_match.group(3)) & 0xFFFFFFFF,
                }
            )

    scan_lines(m0_path, on_line)
    unique_addresses = sorted(
        dedupe(addresses, lambda item: f"{item['addressHash']}:{item['bundleHash']}:{item['address']}"),
        key=lambda item: item["address"],
    )
    unique_sprites = sorted(
        dedupe(sprites, lambda item: f"{item['spriteAddress']}:{item['atlasAddress']}:{item['spriteName']}"),
        key=lambda item: item["spriteName"],
    )
    return {
        "m0Path": str(m0_path),
        "addressCount": len(unique_addresses),
        "spriteCount": len(unique_sprites),
        "addresses": unique_addresses,
        "sprites": unique_sprites,
    }


def scan_lines(file_path: Path, on_line: Any) -> None:
    file_size = file_path.stat().st_size
    emitted: set[bytes] = set()
    carry = b""
    position = 0
    last_percent = -1

    with file_path.open("rb") as file:
        while True:
            chunk = file.read(CHUNK_SIZE)
            if not chunk:
                break
            position += len(chunk)
            data = carry + chunk
            process_hit_chunk(data, emitted, on_line)
            carry = data[-MAX_LINE_BYTES:]
            percent = int((position / file_size) * 100)
            if percent >= last_percent + 10:
                last_percent = percent
                print(f"m0 scan {percent}%")
        if carry:
            process_hit_chunk(carry, emitted, on_line)


def process_hit_chunk(data: bytes, emitted: set[bytes], on_line: Any) -> None:
    lower = data.lower()
    for hit in hit_positions(lower):
        address_at = lower.rfind(b"address:", 0, hit + 1)
        sprite_at = lower.rfind(b"spriteaddress:", 0, hit + 1)
        found = max(address_at, sprite_at)
        if found < 0 or hit - found > MAX_LINE_BYTES:
            continue

        end = find_line_end(data, found)
        if hit > end:
            continue

        line_bytes = data[found:end]
        if len(line_bytes) > MAX_LINE_BYTES or not looks_ascii(line_bytes):
            continue
        if b" ->>>> hash:" not in line_bytes and b"-->spriteName:" not in line_bytes:
            continue
        if line_bytes in emitted:
            continue
        emitted.add(line_bytes)
        on_line(line_bytes.decode("utf8", errors="ignore"))


def hit_positions(lower: bytes):
    for term in HIT_TERMS:
        offset = 0
        while True:
            found = lower.find(term, offset)
            if found < 0:
                break
            yield found
            offset = found + len(term)


def find_line_end(data: bytes, start: int) -> int:
    max_index = min(len(data), start + MAX_LINE_BYTES + 1)
    for index in range(start, max_index):
        if data[index] in {0, 10, 13}:
            return index
    return max_index


def looks_ascii(data: bytes) -> bool:
    for byte in data:
        if byte == 9:
            continue
        if byte < 32 or byte > 126:
            return False
    return True


def has_hit(value: str) -> bool:
    lowered = value.lower()
    return "emoji" in lowered or "sticker" in lowered


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    address = normalize(record.get("address", ""))
    return {
        **record,
        "address": address,
        "basename": normalize(record.get("basename") or posix_basename(address)),
        "addressHash": int(record.get("addressHash") or 0) & 0xFFFFFFFF,
        "bundleHash": int(record.get("bundleHash") or 0) & 0xFFFFFFFF,
    }


def classify_record(record: dict[str, Any]) -> dict[str, Any]:
    address = record["address"]
    match = re.match(r"^ui/textures/chat_emoji/([^/]+)$", address, re.I)
    if match:
        return {"category": "chat-texture", "family": "chat", "locale": "", "asset": match.group(1)}

    match = re.match(r"^ui/localizetextures/([^/]+)/chat_emoji/([^/]+)$", address, re.I)
    if match:
        return {
            "category": "localized-chat-texture",
            "family": "chat",
            "locale": match.group(1).lower(),
            "asset": match.group(2),
        }

    match = re.match(r"^ui/textures/photograph_decoration/stickers/sticker_(\d+)$", address, re.I)
    if match:
        return {
            "category": "photo-sticker-texture",
            "family": "photo",
            "locale": "",
            "asset": record["basename"],
            "photoStickerId": int(match.group(1)),
        }

    match = re.match(r"^ui/localizetextures/([^/]+)/photograph_decoration/stickers/sticker_(\d+)$", address, re.I)
    if match:
        return {
            "category": "localized-photo-sticker-texture",
            "family": "photo",
            "locale": match.group(1).lower(),
            "asset": record["basename"],
            "photoStickerId": int(match.group(2)),
        }

    match = re.match(r"^(item_icons_chat_sticker_.+)$", address, re.I)
    if match:
        return {
            "category": "item-chat-sticker-icon",
            "family": "item",
            "locale": "",
            "asset": record["basename"],
            "itemIconPath": match.group(1),
        }

    match = re.match(r"^ui/emoji/([^/]+)$", address, re.I)
    if match:
        return {"category": "ui-emoji-related", "family": "ui", "locale": "", "asset": match.group(1)}

    if re.match(r"^ui/prefabs/", address, re.I):
        return {"category": "ui-prefab-related", "family": "prefab", "locale": "", "asset": record["basename"]}

    if re.search(r"home_interior_furniture_sticker", address, re.I):
        return {"category": "home-furniture-sticker-related", "family": "furniture", "locale": "", "asset": record["basename"]}

    return {"category": "other-sticker-emoji-related", "family": "other", "locale": "", "asset": record["basename"]}


def identify_record(
    record: dict[str, Any],
    classified: dict[str, Any],
    token_context: dict[str, Any],
    item_index: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    related_chat_asset = related_chat_asset_for(record, classified)
    token = infer_chat_token(related_chat_asset, token_context) if related_chat_asset else empty_token()
    item_icon_path = classified.get("itemIconPath", "")
    item_matches = item_index.get(item_icon_path, []) if item_icon_path else []
    primary_item = select_primary_item(item_matches)
    return {
        "relatedChatAsset": related_chat_asset,
        **token,
        "stickerSyntax": f"[sticker: {token['chatToken']}]" if token["chatToken"] and token["kind"] != "emoji" else "",
        "emojiPicSyntax": f"emojiPic=%s=%s{token['chatToken']}" if token["chatToken"] else "",
        "photoStickerId": classified.get("photoStickerId"),
        "furnitureStickerId": infer_furniture_sticker_id(record["basename"]),
        "itemIconPath": item_icon_path,
        "primaryItem": primary_item,
        "primaryItemName": item_name(primary_item),
        "itemIds": item_matches,
        "itemNames": [item_name(item) for item in item_matches if item_name(item)],
    }


def related_chat_asset_for(record: dict[str, Any], classified: dict[str, Any]) -> str:
    if classified["family"] == "chat":
        return classified.get("asset", "")
    match = re.match(r"^item_icons_chat_(sticker_.+)$", record["basename"], re.I)
    if match:
        return match.group(1)
    return ""


def build_token_context(records: list[dict[str, Any]]) -> dict[str, Any]:
    emoji_indices = []
    for record in records:
        if not re.match(r"^ui/textures/chat_emoji/", record["address"], re.I):
            continue
        match = re.match(r"^emoji_2_(\d+)$", record["basename"], re.I)
        if match:
            emoji_indices.append(int(match.group(1)))
    emoji_rank = {index: offset + 1 for offset, index in enumerate(sorted(emoji_indices))}
    return {"emoji2RankByIndex": emoji_rank}


def infer_chat_token(asset: str, context: dict[str, Any]) -> dict[str, Any]:
    match = re.match(r"^emoji_2_(\d+)$", asset, re.I)
    if match:
        index = int(match.group(1))
        rank = context["emoji2RankByIndex"].get(index)
        if rank:
            chat_id = 6000 + rank
            return {
                "kind": "emoji",
                "chatToken": f"{asset}{chat_id}",
                "chatId": chat_id,
                "confidence": "observed-pattern" if index in {1, 2, 5} else "inferred-visible-order",
                "reason": "emoji_2 uses visible asset order; observed examples include 1->6001, 2->6002, 5->6004",
            }

    match = re.match(r"^sticker_(\d+)_(\d+)$", asset, re.I)
    if match:
        group = int(match.group(1))
        index = int(match.group(2))
        base = numeric_sticker_base(group)
        if base is not None:
            chat_id = base + index
            return {
                "kind": "sticker",
                "chatToken": f"{asset}{chat_id}",
                "chatId": chat_id,
                "confidence": "observed-pattern" if group in {3, 4} else "series-inferred",
                "reason": numeric_sticker_reason(group),
            }

    match = re.match(r"^sticker_(ip\d+)_(\d+)$", asset, re.I)
    if match:
        group = match.group(1).lower()
        index = int(match.group(2))
        base = ip_sticker_base(group)
        if base is not None:
            chat_id = base + index
            return {
                "kind": "sticker",
                "chatToken": f"{asset}{chat_id}",
                "chatId": chat_id,
                "confidence": "observed-pattern" if group == "ip001" else "series-inferred",
                "reason": "matches observed sticker_ip001 examples" if group == "ip001" else "IP sticker series inferred from pack order after ip001",
            }

    return empty_token("no known or inferred chat-token rule for this asset")


def empty_token(reason: str = "") -> dict[str, Any]:
    return {"kind": "", "chatToken": "", "chatId": None, "confidence": "unknown", "reason": reason}


def numeric_sticker_base(group: int) -> int | None:
    return {1: 7000, 3: 7999, 4: 9000, 5: 10000, 6: 11000, 7: 12000}.get(group)


def numeric_sticker_reason(group: int) -> str:
    if group == 3:
        return "matches observed sticker_3 examples; this series is offset because sticker_3_1 is absent"
    if group == 4:
        return "matches observed sticker_4 examples"
    return "numeric sticker series inferred from neighboring sticker packs"


def ip_sticker_base(group: str) -> int | None:
    return {"ip001": 13000, "ip003": 14000, "ip004": 15000}.get(group)


def build_image_plan(record: dict[str, Any], classified: dict[str, Any], export_root: Path) -> dict[str, Any] | None:
    if not should_attempt_decode(classified):
        return None
    asset_id = safe_asset_id(record["address"])
    return {
        "AssetId": asset_id,
        "ResourceType": "Texture2D",
        "TextureName": record["basename"],
        "AllowFirstTextureFallback": False,
        "PngFile": str(export_root / "emoicons" / f"{asset_id}.png"),
    }


def should_attempt_decode(classified: dict[str, Any]) -> bool:
    if classified["category"] == "ui-emoji-related":
        return classified.get("asset") in {"emoji_data", "emoji_tex"}
    return classified["category"] in {
        "chat-texture",
        "localized-chat-texture",
        "photo-sticker-texture",
        "localized-photo-sticker-texture",
        "item-chat-sticker-icon",
    }


def skip_decode_reason(classified: dict[str, Any]) -> str:
    if classified["category"] == "ui-emoji-related":
        return "related UI asset preserved in manifest; no Texture2D export expected"
    if classified["category"] == "ui-prefab-related":
        return "UI prefab/template reference, not a direct texture"
    if classified["category"] == "home-furniture-sticker-related":
        return "home furniture sticker prefab/mesh/ECS related asset, not a direct texture"
    return ""


def decode_images(
    container_dir: Path,
    meta_entries: dict[int, MetaEntry],
    plan_items: list[dict[str, Any]],
    dry_run: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    try:
        import UnityPy
    except ModuleNotFoundError as error:
        raise RuntimeError("Missing UnityPy. Install with: python -m pip install UnityPy Pillow texture2ddecoder") from error

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in plan_items:
        grouped[int(item["BundleHash"]) & 0xFFFFFFFF].append(item)

    exported: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    with TemporaryDirectory(prefix="bpsr_emoicon_bundles_", ignore_cleanup_errors=True) as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        total = len(grouped)
        for number, (bundle_hash, bundle_items) in enumerate(grouped.items(), start=1):
            if number == 1 or number == total or number % 50 == 0:
                print(f"decode bundle {number}/{total}")

            entry = meta_entries.get(bundle_hash)
            if entry is None:
                for item in bundle_items:
                    failed.append(failure(item, f"Missing package entry for bundleHash {bundle_hash}"))
                continue

            bundle_file = temp_dir / f"{bundle_hash}.bundle"
            try:
                bundle_file.write_bytes(read_pkg_entry(container_dir, entry))
                bundle = read_bundle(UnityPy, bundle_file)
            except Exception as error:
                for item in bundle_items:
                    failed.append(failure(item, str(error)))
                continue

            for item in bundle_items:
                try:
                    selected = select_image(bundle, item)
                    image = selected["image"]
                    if image.mode != "RGBA":
                        image = image.convert("RGBA")

                    png_path = Path(item["PngFile"])
                    if not dry_run:
                        png_path.parent.mkdir(parents=True, exist_ok=True)
                        image.save(png_path)

                    exported.append(
                        {
                            "AssetId": item["AssetId"],
                            "PngFile": str(png_path),
                            "PngWidth": image.width,
                            "PngHeight": image.height,
                            "PngMode": image.mode,
                            "PixelSha256": hashlib.sha256(image.tobytes()).hexdigest(),
                            "ObjectPathId": str(selected["path_id"]) if selected.get("path_id") is not None else None,
                            "SelectedObjectName": selected.get("name", ""),
                            "SelectedObjectType": selected.get("type", ""),
                        }
                    )
                except Exception as error:
                    failed.append(failure(item, str(error)))

    return {"Exported": exported, "Failed": failed}


def read_pkg_entry(container_dir: Path, entry: MetaEntry) -> bytes:
    pkg_path = container_dir / f"m{entry.index}.pkg"
    with pkg_path.open("rb") as file:
        file.seek(entry.offset)
        return file.read(entry.length)


def read_bundle(UnityPy: Any, bundle_file: Path) -> dict[str, Any]:
    env = UnityPy.load(str(bundle_file))
    sprites: dict[str, Any] = {}
    textures: dict[str, Any] = {}
    first_texture = None

    for obj in env.objects:
        if obj.type.name == "Sprite":
            data = obj.read()
            name = getattr(data, "m_Name", "") or getattr(data, "name", "")
            if name and name not in sprites:
                sprites[name] = {"image": data.image, "path_id": obj.path_id, "name": name, "type": "Sprite"}
        elif obj.type.name == "Texture2D":
            data = obj.read()
            name = getattr(data, "m_Name", "") or getattr(data, "name", "")
            record = {"image": data.image, "path_id": obj.path_id, "name": name, "type": "Texture2D"}
            if first_texture is None:
                first_texture = record
            if name and name not in textures:
                textures[name] = record

    return {"sprites": sprites, "textures": textures, "first_texture": first_texture}


def select_image(bundle: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    if item.get("ResourceType") == "Sprite":
        sprite_name = item.get("SpriteName", "")
        selected = bundle["sprites"].get(sprite_name)
        if selected is None:
            raise ValueError(f"Sprite not found in bundle: {sprite_name}")
        return selected

    texture_name = item.get("TextureName", "")
    if texture_name and texture_name in bundle["textures"]:
        return bundle["textures"][texture_name]
    if bundle["first_texture"] is not None and item.get("AllowFirstTextureFallback", True):
        return bundle["first_texture"]
    raise ValueError("Texture2D image not found in bundle")


def failure(item: dict[str, Any], error: str) -> dict[str, Any]:
    return {
        "AssetId": item.get("AssetId", ""),
        "PngFile": item.get("PngFile", ""),
        "ResourceType": item.get("ResourceType", ""),
        "SpriteName": item.get("SpriteName", ""),
        "TextureName": item.get("TextureName", ""),
        "Error": error,
    }


def load_item_index(file_path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        items = json.loads(file_path.read_text(encoding="utf8"))
    except OSError:
        return {}

    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        icon_path = normalize(item.get("IconPath", ""))
        if re.match(r"^item_icons_chat_sticker_", icon_path, re.I):
            index[icon_path].append(item)

    for rows in index.values():
        rows.sort(key=lambda item: int(item.get("Id") or 0))
    return dict(index)


def find_item_names_json() -> Path | None:
    bases = [Path.cwd(), executable_dir()]
    bases.extend(executable_dir().parents)
    bases.extend(Path.cwd().parents)
    candidates: list[Path] = []
    for base in unique_paths(bases):
        candidates.extend(base.glob("BPSR-UID-Extractors/output-build-*/itemnames.json"))
        candidates.extend(base.glob("BPSR-UID-Extractors/output/itemnames.json"))
    candidates = [path for path in unique_paths(candidates) if path.is_file()]
    if not candidates:
        return None
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0]


def select_primary_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    for item in items:
        item_id = int(item.get("Id") or 0)
        if 1078000 <= item_id < 1079000:
            return item
    return items[0]


def item_name(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    names = item.get("Names") or {}
    return str(names.get("en") or item.get("NameDesign") or "")


def infer_furniture_sticker_id(asset: str) -> int | None:
    match = re.search(r"home_interior_furniture_sticker(\d+)_a", asset, re.I)
    return int(match.group(1)) if match else None


def build_summary(
    m0_path: Path,
    item_names_path: Path | None,
    records: list[dict[str, Any]],
    plan_items: list[dict[str, Any]],
    manifest_items: list[dict[str, Any]],
    missing: list[str],
    started: float,
) -> dict[str, Any]:
    return {
        "GeneratedAt": now_iso(),
        "M0Path": str(m0_path),
        "ItemNamesPath": str(item_names_path) if item_names_path else "",
        "TotalAddressCandidates": len(records),
        "DecodeAttemptCount": len(plan_items),
        "PngExportedCount": sum(1 for item in manifest_items if item["pngExported"]),
        "PngFailureCount": sum(1 for item in manifest_items if item["pngError"]),
        "MissingPackageEntryCount": len(missing),
        "UniqueBundlesExtracted": len({item["BundleHash"] for item in plan_items}),
        "ChatTokenCount": len({item["chatToken"] for item in manifest_items if item["category"] == "chat-texture" and item["chatToken"]}),
        "PhotoStickerTextureCount": sum(1 for item in manifest_items if item["category"] == "photo-sticker-texture"),
        "ItemChatStickerIconCount": sum(1 for item in manifest_items if item["category"] == "item-chat-sticker-icon"),
        "CategoryCounts": dict(sorted(Counter(item["category"] for item in manifest_items).items())),
        "FamilyCounts": dict(sorted(Counter(item["family"] for item in manifest_items).items())),
        "ChatIdConfidenceCounts": dict(sorted(Counter(item["idConfidence"] for item in manifest_items if item["chatToken"]).items())),
        "ElapsedSeconds": round(time.time() - started, 2),
    }


def write_candidates(export_root: Path, m0_path: Path, candidates: dict[str, Any]) -> None:
    write_json(export_root / "all_emoji_sticker_candidates.json", {"m0Path": str(m0_path), **candidates})
    lines = [f"# Addresses: {len(candidates['addresses'])}"]
    for item in candidates["addresses"]:
        lines.append(
            "\t".join(
                [
                    "address",
                    str(item.get("packageIndex") or ""),
                    str(item.get("bundleHash") or ""),
                    str(item.get("addressHash") or ""),
                    item.get("address", ""),
                ]
            )
        )
    lines.append("")
    lines.append(f"# Sprites: {len(candidates['sprites'])}")
    for item in candidates["sprites"]:
        lines.append(
            "\t".join(
                [
                    "sprite",
                    str(item.get("atlasAddress") or ""),
                    str(item.get("spriteAddress") or ""),
                    item.get("spriteName", ""),
                ]
            )
        )
    (export_root / "all_emoji_sticker_candidates.txt").write_text("\n".join(lines) + "\n", encoding="utf8")


def write_code_files(out_dir: Path, items: list[dict[str, Any]]) -> None:
    chat_rows = sorted(
        [item for item in items if item["category"] == "chat-texture" and item["chatToken"]],
        key=lambda item: sort_key(item["relatedChatAsset"]),
    )
    unique_rows = dedupe(chat_rows, lambda item: item["chatToken"])
    write_lines(
        out_dir / "chat_codes_unique_mixed.txt",
        [
            item["emojiPicSyntax"] if item["relatedChatAsset"].startswith("emoji_") else item["stickerSyntax"]
            for item in unique_rows
        ],
    )
    write_lines(
        out_dir / "chat_codes_unique_sticker_syntax.txt",
        [item["stickerSyntax"] for item in unique_rows if not item["relatedChatAsset"].startswith("emoji_")],
    )
    write_lines(out_dir / "chat_codes_unique_emojiPic_syntax.txt", [item["emojiPicSyntax"] for item in unique_rows])
    write_lines(
        out_dir / "chat_tokens.tsv",
        [
            "chatToken\tchatId\tasset\tkind\tconfidence",
            *[
                "\t".join(
                    [
                        str(item["chatToken"]),
                        str(item["chatId"]),
                        str(item["relatedChatAsset"]),
                        "emoji" if item["relatedChatAsset"].startswith("emoji_") else "sticker",
                        str(item["idConfidence"]),
                    ]
                )
                for item in unique_rows
            ],
        ],
    )


def build_contact_sheets(export_root: Path, items: list[dict[str, Any]]) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ModuleNotFoundError:
        print("Pillow not available; skipped contact sheets.")
        return

    font = ImageFont.load_default()
    groups = [
        (
            "contact_sheet_chat_textures.png",
            [item for item in items if item["category"] == "chat-texture" and item["pngExported"]],
            lambda item: [item["asset"], item["chatToken"] or item["category"]],
            8,
        ),
        (
            "contact_sheet_photo_stickers.png",
            [item for item in items if item["category"] == "photo-sticker-texture" and item["pngExported"]],
            lambda item: [item["asset"], f"photoStickerId={item['photoStickerId']}"],
            8,
        ),
        (
            "contact_sheet_item_icons.png",
            [item for item in items if item["category"] == "item-chat-sticker-icon" and item["pngExported"]],
            lambda item: [item["asset"], f"item={item['primaryItemId'] or ''}", item["primaryItemName"]],
            6,
        ),
        (
            "contact_sheet_localized_chat.png",
            [item for item in items if item["category"] == "localized-chat-texture" and item["pngExported"]],
            lambda item: [item["locale"], item["asset"], item["chatToken"]],
            8,
        ),
        (
            "contact_sheet_localized_photo.png",
            [item for item in items if item["category"] == "localized-photo-sticker-texture" and item["pngExported"]],
            lambda item: [item["locale"], f"photoStickerId={item['photoStickerId']}"],
            5,
        ),
        (
            "contact_sheet_all_decoded.png",
            [item for item in items if item["pngExported"]],
            lambda item: [item["category"], item["asset"]],
            8,
        ),
    ]
    for filename, group_items, label_fn, columns in groups:
        if not group_items:
            continue
        write_contact_sheet(export_root, filename, sorted(group_items, key=lambda item: sort_key(item["assetAddress"])), label_fn, columns, font, Image, ImageDraw)


def write_contact_sheet(
    export_root: Path,
    filename: str,
    items: list[dict[str, Any]],
    label_fn: Any,
    columns: int,
    font: Any,
    Image: Any,
    ImageDraw: Any,
) -> None:
    thumb_size = 112
    tile_w = 176
    tile_h = 174
    margin = 14
    rows = max(1, (len(items) + columns - 1) // columns)
    sheet = Image.new("RGBA", (columns * tile_w + margin * 2, rows * tile_h + margin * 2), (245, 245, 245, 255))
    draw = ImageDraw.Draw(sheet)

    for index, item in enumerate(items):
        col = index % columns
        row = index // columns
        x = margin + col * tile_w
        y = margin + row * tile_h
        png_path = export_root / item["pngFile"]
        try:
            image = Image.open(png_path).convert("RGBA")
        except Exception:
            continue

        image.thumbnail((thumb_size, thumb_size), Image.Resampling.LANCZOS)
        px = x + (tile_w - image.width) // 2
        py = y + 4 + (thumb_size - image.height) // 2
        sheet.alpha_composite(image, (px, py))

        label_y = y + thumb_size + 10
        for line in contact_label_lines(label_fn(item)):
            draw.text((x + 6, label_y), line, fill=(20, 20, 20, 255), font=font)
            label_y += 12

    out_path = export_root / filename
    sheet.convert("RGB").save(out_path, quality=95)
    print(f"Wrote {out_path.name} ({len(items)} items)")


def contact_label_lines(values: list[Any]) -> list[str]:
    lines: list[str] = []
    for value in values:
        text = ascii_text(value)
        if not text:
            continue
        lines.extend(textwrap.wrap(text, width=24)[:2])
        if len(lines) >= 4:
            break
    return lines[:4]


def write_readme(path: Path, summary: dict[str, Any]) -> None:
    lines = [
        "# BPSR emoji/sticker export",
        "",
        "Generated by BPSR Emoicon Scanner.",
        "",
        "## Counts",
        "",
        f"- Address candidates: {summary['TotalAddressCandidates']}",
        f"- PNG decode attempts: {summary['DecodeAttemptCount']}",
        f"- PNGs exported: {summary['PngExportedCount']}",
        f"- PNG export failures: {summary['PngFailureCount']}",
        f"- Unique chat tokens from primary chat textures: {summary['ChatTokenCount']}",
        f"- Photo sticker textures: {summary['PhotoStickerTextureCount']}",
        f"- Chat sticker item icons: {summary['ItemChatStickerIconCount']}",
        "",
        "## Files",
        "",
        "- `emoicons/`: decoded PNG files named by full asset address.",
        "- `all_emoji_sticker_manifest.csv`: spreadsheet-friendly manifest.",
        "- `all_emoji_sticker_manifest.json`: full manifest.",
        "- `codes/chat_codes_unique_mixed.txt`: primary chat syntax.",
        "- `codes/chat_codes_unique_sticker_syntax.txt`: `[sticker: ...]` syntax only.",
        "- `codes/chat_codes_unique_emojiPic_syntax.txt`: `emojiPic=%s=%s...` syntax for every chat token.",
        "- `contact_sheet_*.png`: visual review sheets.",
        "",
        "## Package source",
        "",
        "The scanner reads `m0.pkg` for the address table and `meta.pkg` for bundle locations.",
        "Each manifest row includes `packageFile`, `packageOffset`, and `packageLength` for traceability.",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf8")


def write_zip(export_root: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()

    include_files: list[Path] = []
    for pattern in [
        "README.md",
        "all_emoji_sticker_candidates.json",
        "all_emoji_sticker_candidates.txt",
        "all_emoji_sticker_export_plan.json",
        "all_emoji_sticker_export_result.json",
        "all_emoji_sticker_manifest.json",
        "all_emoji_sticker_manifest.csv",
        "all_emoji_sticker_summary.json",
        "contact_sheet_*.png",
    ]:
        include_files.extend(export_root.glob(pattern))

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for directory in [export_root / "emoicons", export_root / "codes"]:
            if not directory.is_dir():
                continue
            for file_path in sorted(directory.rglob("*")):
                if file_path.is_file():
                    archive.write(file_path, file_path.relative_to(export_root))
        for file_path in sorted(set(include_files)):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(export_root))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    headers = [
        "asset",
        "assetAddress",
        "category",
        "family",
        "locale",
        "relatedChatAsset",
        "chatToken",
        "chatId",
        "idConfidence",
        "stickerSyntax",
        "emojiPicSyntax",
        "photoStickerId",
        "furnitureStickerId",
        "itemIconPath",
        "primaryItemId",
        "primaryItemName",
        "itemIds",
        "itemNames",
        "decodeAttempted",
        "decodeSkippedReason",
        "pngExported",
        "pngFile",
        "pngWidth",
        "pngHeight",
        "pixelSha256",
        "selectedObjectName",
        "selectedObjectType",
        "bundleHash",
        "addressHash",
        "packageIndex",
        "packageFile",
        "packageOffset",
        "packageLength",
        "pngError",
        "idReason",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({header: csv_value(row.get(header)) for header in headers})


def write_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(line for line in lines if line) + "\n", encoding="utf8")


def prepare_export_root(export_root: Path) -> None:
    if export_root.exists():
        shutil.rmtree(export_root)
    export_root.mkdir(parents=True, exist_ok=True)


def csv_value(value: Any) -> Any:
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return "" if value is None else value


def dedupe(items: Any, key_fn: Any) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for item in items:
        key = str(key_fn(item))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def record_key(record: dict[str, Any]) -> str:
    return f"{record['addressHash']}:{record['bundleHash']}:{record['address']}"


def compare_records(record: dict[str, Any]) -> list[Any]:
    classified = classify_record(record)
    return [classified["category"], *sort_key(record["address"])]


def sort_key(value: Any) -> list[Any]:
    chunks = re.split(r"(\d+)", str(value or "").lower())
    return [int(chunk) if chunk.isdigit() else chunk for chunk in chunks if chunk != ""]


def safe_asset_id(address: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "__", normalize(address), flags=re.I).strip("_")


def relative_path(file_path: str | Path, root: Path) -> str:
    return Path(file_path).resolve().relative_to(root.resolve()).as_posix()


def normalize(value: Any) -> str:
    return str(value or "").replace("\\", "/").replace("\0", "").strip()


def posix_basename(value: str) -> str:
    return normalize(value).rsplit("/", 1)[-1]


def ascii_text(value: Any) -> str:
    return str(value or "").encode("ascii", "replace").decode("ascii")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    raise SystemExit(main())
