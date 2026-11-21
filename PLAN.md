# Flypage — Implementation Plan

Flypage detects a scanner with internal memory (e.g. IRIScan Book) when it is
plugged in, imports its files, lets the user sort them into named directories,
and produces searchable PDFs (image + invisible OCR text layer).

See [README.md](README.md) for the original requirements.

---

## 1. Decisions

| Topic | Decision |
| --- | --- |
| Language | **Python 3** (EasyOCR is Python-only; ocrmypdf is Python) |
| GUI toolkit | **GTK 4 + libadwaita** via PyGObject |
| Tesseract backend | `ocrmypdf` + `tesseract-ocr-*` |
| EasyOCR backend | **Dropped** (evaluated 2026-11-19, see §7.1): on scanned documents Tesseract is as accurate or better and 10–60× faster on the target laptop; EasyOCR only wins on scene text (posters, signs), which a document scanner does not produce |
| Image → PDF | `img2pdf` (lossless) |
| Scanner detection | Per-user session daemon using **UDisks2 (D-Bus) + `udevadm`** — no root |
| Build / packaging | Meson + gettext + `debian/` (debhelper, dh-python) |
| Config storage | `~/.config/flypage/config.toml` (`tomllib` read, `python3-tomli-w` write) |
| Delete button | Deletes the file **from the scanner memory** (and from staging) |
| Originals | **Removed** after a successful conversion — only the OCR'd PDF is kept |
| Merging | A **Chain** button links the selected files into one merge group → a single PDF; unchained files give one PDF each |
| Sorting mode | A **two-state toggle**: **"Same for all"** or **"Per file"** |
| Destinations | **One row of icons**: Default, then the user's directories |
| Launch | **Dragging files onto a directory icon** assigns them and starts their OCR right away — no Launch button |
| Overview | All imported files are shown as **thumbnails** so the user can decide |
| Page order | **Drag-and-drop of thumbnails** inside the grid sets the page order of a chain |
| Re-import | Files still on the scanner (not deleted) **show up again** at the next import — no "already imported" tracking |
| Output naming | Output PDFs are named from a **template with `%` codes** (e.g. `%Y-%m-%d-ocr-%n`) set in the Settings GUI |

---

## 2. Project layout

```text
flypage/
├── meson.build, meson_options.txt
├── flypage/                    # Python package
│   ├── __main__.py             # CLI entry: settings | daemon | sort | process
│   ├── application.py          # Adw.Application
│   ├── config.py               # load/save TOML, defaults, validation
│   ├── scanners.py             # scanner profiles, USB matching
│   ├── daemon.py               # plug/unplug watcher (UDisks2 + udevadm)
│   ├── device.py               # mount rw/ro, list files, delete on device, unmount
│   ├── importer.py             # copy device → staging
│   ├── model.py                # ImportedFile, Document (1..n files), Session
│   ├── thumbnails.py           # async thumbnail generation + cache
│   ├── naming.py               # file name template (% codes): rendering + validation
│   ├── pipeline.py             # image→PDF, merge, OCR dispatch, progress, cancel
│   ├── ocr/
│   │   ├── base.py             # OcrEngine interface + registry/availability
│   │   └── tesseract.py            # (easyocr.py dropped, see §7.1)
│   ├── ui/
│   │   ├── settings_window.py
│   │   ├── process_window.py
│   │   ├── thumbnail_grid.py
│   │   └── destination_bar.py
│   └── i18n.py
├── data/
│   ├── ui/*.blp or *.ui         # GtkBuilder (optionally Blueprint)
│   ├── icons/                   # app icon + folder icon set for destinations
│   ├── io.github.flypage.Settings.desktop   # system menu entry
│   ├── io.github.flypage.Daemon.desktop     # installed in /etc/xdg/autostart
│   ├── flypage-daemon.service               # optional systemd --user unit
│   └── io.github.flypage.metainfo.xml
├── po/                          # POTFILES, LINGUAS, flypage.pot, fr.po
├── debian/                      # control, rules, changelog, copyright, *.install
└── tests/
```

---

## 3. Scanner detection (replaces the current root scripts)

The existing `99-scanner.rules` / `scan-import@.service` / `scan-import.sh`
run as root, need `runuser`/`DISPLAY` hacks to reach the user session, and set
`UDISKS_IGNORE=1`, which hides the device from the desktop. They will be
**removed**.

Design (implemented in milestone 5):

1. `flypage daemon` (`flypage/daemon.py`, a `Gio.Application` without GTK,
   unique per session) starts with the session via XDG autostart
   (`/etc/xdg/autostart/io.github.flypage.Daemon.desktop`).
2. `flypage/device.py` watches UDisks2 with a `Gio.DBusObjectManagerClient`
   (devices already plugged at start count as plugged).
3. For each partition with a filesystem, the USB IDs (`ID_VENDOR_ID`,
   `ID_MODEL_ID`, `ID_SERIAL_SHORT`) come from `udevadm info` (no GUdev
   dependency) and are matched against the profiles (`flypage/scanners.py`;
   empty model/serial = any).
4. On match: wait up to 4 s for the desktop's automount, else mount it via
   UDisks2 (read-write, needed to delete); apply the **pending deletions**
   of that scanner; copy all files under `source_dir` (hidden folders
   skipped) to `~/.cache/flypage/incoming/<time>-<scanner>/` with a
   `manifest.json` (copy → path on the scanner, size, mtime); then start
   `flypage import-window <staging>` as a separate process. Notifications:
   *detected / copying*, *nothing to import*, or the error.
5. The processing window deletes an original on the scanner after its PDF
   is written (if *delete after import*) and on *Delete* — only if it still
   has the imported size and mtime. If the scanner is no longer mounted,
   the deletion goes to `~/.local/state/flypage/pending-deletions.json`
   (file-locked: several processes) and is done at the next plug.
6. On unplug during the copy: abort, remove the partial staging folder, no
   notification. Once the window is open it works from staging; closing it
   (or SIGTERM at logout) removes the staging folder — the scanner keeps the
   files that were not processed, and they show up again next time.
7. Settings → Scanners → add/edit: a *Detect* list shows the USB storage
   devices as they are plugged in; *Use* fills name, vendor/model IDs and
   suggests `DCIM` when the memory has one.

---

### Observed on a real IRIScan Book (2026-11-18)

- USB mass storage, reported as **`OLYMPUS FE210/X775`, `07b4:0109`**
  (OEM controller; `lsusb`'s name for it is misleading). Serial
  `964500000000100` looks like a constant shared by units: not used to match.
- One FAT32 partition (≈4 GB, no label), **auto-mounted read-write by the
  desktop** at `/media/<user>/<UUID>` (UDisks2 `HintAuto=true`). The daemon
  must reuse an existing mount instead of mounting again.
- Scans are in **`DCIM/100MEDIA/IMAGnnnn.JPG|PDF`**. The root also has
  `System Volume Information/` and **`.Trash-1000/`**: files "deleted" from a
  file manager still fill the memory there. Import only from `source_dir`,
  and *Delete* must really unlink (never move to trash).
- PDFs (Creator "Handy Scanner") are 1 page, A4, one JPEG image of up to
  5120×6176 px, no text. JPGs are 300 dpi, EXIF has `DateTime` but no
  `DateTimeOriginal` (so `%{scan_date}` uses the file time).
- **The JPEG data is slightly malformed**: Pillow and viewers decode it, but
  libjpeg/qpdf warn "JPEG data is corrupt", and ocrmypdf ends with exit
  code 4 (*output invalid*) although the OCR'd PDF is fine. The Tesseract
  engine accepts exit code 4 when the output opens with the right page
  count (the image is the scanner's own, passed through unchanged).
- OCR takes **≈35 s per page** at this resolution (one core). §7.1 has
  measured options that cut it by ≈30 % with no loss of accuracy.

## 4. Configuration (`config.toml`)

```toml
ui_language = "system"           # "system" | "en" | "fr"

[ocr]
engine = "tesseract"             # "tesseract" | "easyocr"
languages = ["eng", "fra"]

[processing]
sort_mode = "per_file"           # "all_same" | "per_file"  (last used, pre-selected)

[naming]
template = "%Y-%m-%d_%{dest}_%3n"  # see §5 "Naming" for the codes

[[scanners]]
name = "IRIScan Book"
vendor_id = "07b4"
model_id = "0109"                # empty = any model of this vendor
serial = ""                      # empty = any unit
source_dir = "DCIM"              # folder on the scanner; empty = whole memory
delete_after_import = true       # remove from scanner once PDFs are produced

[[destinations]]
name = "Invoices"
path = "~/Documents/Invoices"
icon = "x-office-spreadsheet-symbolic"   # theme icon name, or an image file path
template = "%Y-%m_%{original}"           # optional: overrides [naming] template

[[destinations]]
name = "Documents"
path = "~/Documents/Scans"
icon = "folder-symbolic"
default = true                   # exactly one default
```

Loading never fails: a bad value (including an invalid template) is logged
and replaced by its default; unknown keys are reported.

---

## 5. Settings window (system menu entry)

`flypage settings` (`flypage/ui/settings_window.py`): an
`Adw.ApplicationWindow` with an `Adw.ViewStack` of five pages and a view
switcher in the header (moved to a bottom bar when the window is narrow).
`Adw.PreferencesWindow` is deprecated, hence not used. **Every valid change
is saved immediately** (300 ms after the last keystroke, and on close); there
is no Save button. Errors are shown as toasts.

- **Destinations**: list of icon + name + folder; radio on the left marks the
  **default**; move up / move down / remove (with *Undo* toast); click a row
  to edit. The edit dialog has name (must be unique), folder (folder
  chooser, stored with `~`), icon (theme icons or an image file) and an
  optional **own file name template**.
- **Naming**: the file name template field (see below).
- **OCR**: engine (EasyOCR shown as *not supported*; to be removed, see
  §7.1 and milestone 7); checkboxes for the
  installed Tesseract languages, shown with their translated names from the
  `iso-codes` package (at least one must stay checked; configured but
  missing languages are flagged *not installed*).
- **Scanners**: list of profiles (name, USB IDs, folder on the scanner,
  *delete after import*), add / edit / remove. IDs are typed by hand for
  now; the **"Plug your scanner now"** wizard that captures them comes with
  the device layer (milestone 5).
- **Interface**: GUI language (System / English / Français), applied at the
  next start.

### File name template

An entry, a live **example** (e.g. `2026-11-18_Invoices_001.pdf`), and a
grid of buttons that insert a code at the cursor. Invalid templates are
flagged in red with the reason and are not saved.

| Code | Value |
| --- | --- |
| `%Y` `%m` `%d` `%H` `%M` `%S` … | processing date/time: any `strftime` code (`%b` month name, `%a` weekday, `%y`…), glibc flags allowed (`%-d`) |
| `%n` / `%3n` | sequence number within the destination for this session; `%3n` zero-pads to 3 digits |
| `%{dest}` | destination name |
| `%{original}` | original file name without extension (first file of a chain) |
| `%{scanner}` | scanner profile name |
| `%{pages}` | page count of the output PDF |
| `%{scan_date}` | date of the original file (EXIF, else mtime), as `YYYY-MM-DD` |
| `%%` | a literal `%` |

Example: `%Y-%m-%d-ocr-%n` → `2026-11-18-ocr-1.pdf`. The `.pdf` extension is
always added; `/` produced by a value or a date code becomes `_`. Name
collisions in the destination get a `-1`, `-2`, … suffix.

---

## 6. Processing window

### 6.1 Window behaviour

- `Adw.ApplicationWindow`, no menu bar, minimal header: an icon + a status
  message (e.g. *"12 files imported from IRIScan Book"*).
- **Not** kept above other windows. When it opens (scanner plugged in), it
  is brought to the front with the keyboard focus: `present()`, plus on X11
  an EWMH `_NET_ACTIVE_WINDOW` request with source "pager" (python-xlib),
  which window managers honour despite focus-stealing prevention
  (`flypage/ui/activation.py`). On Wayland, `present()` only.
- Desktop notifications (`flypage/notifications.py`):
  - daemon: *<scanner> detected — copying its files…*, *nothing to import*,
    or the import error;
  - end of session: *N searchable PDFs created* with the folders, and up to
    three *Open <folder>* buttons. For a scanner session the window prints
    a one-line JSON report on its stdout when the session ends (or when it is
    closed early, or at logout) and the **daemon** sends the notification, so
    the buttons still work after the window has closed. `flypage sort`
    windows send it themselves, without buttons.

### 6.2 Layout

```text
┌──────────────────────────────────────────────────────────────┐
│ [scanner]  12 files imported — choose where to send them     │  message bar
├───────────────────────┬──────────────────────────────────────┤
│ ┌───┐┌───┐┌───┐┌───┐  │                                      │
│ │ 1 ││ 2 ││ 3 ││ 4 │  │                                      │
│ └───┘└───┘└───┘└───┘  │          large preview of the        │
│ ┌───┐┌───┐┌───┐┌───┐  │          selected file               │
│ │ 5 ││ 6 ││ 7 ││ 8 │  │          (zoom to fit)               │
│ └───┘└───┘└───┘└───┘  │                                      │
│   thumbnail grid      │                                      │
│ (badge = destination, │                                      │
│  chain = merge group) │                                      │
├───────────────────────┴──────────────────────────────────────┤
│ [🔗 Chain] [🗑 Delete]        Mode: [ Same for all | Per file ] │
│ [★ Default] [📁 Invoices] [📁 Medical] [📁 Taxes]  ← drop here │
├──────────────────────────────────────────────────────────────┤
│ [Cancel]   ▓▓▓▓▓▓▓▓░░░░░░░░░░░  OCR 5 / 12                    │
└──────────────────────────────────────────────────────────────┘
```

- **Action row**: *Chain* and *Delete* side by side (left); the *Mode*
  toggle on the right.
- **Destination row**: a single line of large icon buttons, each with its
  name underneath — *Default* first, then the configured directories in
  their Settings order. If they do not fit, the row scrolls horizontally.
- **Bottom row**: *Cancel* and the progress bar only.

### 6.3 Thumbnail grid

- `Gtk.GridView` backed by a `Gio.ListStore` of `ImportedFile` objects,
  with `Gtk.MultiSelection` (click, Ctrl+click, Shift+click, Ctrl+A).
- Thumbnails generated in a background thread (Pillow for PNG/JPEG,
  Poppler GI for page 1 of a PDF), cached in `~/.cache/flypage/thumbs/`,
  placeholder spinner until ready.
- Each thumbnail shows overlays: the **destination icon** once assigned,
  a **chain number/colour** if part of a chain, page count for PDFs.
- Selecting a single file shows it large in the preview pane; double-click
  toggles a full-window preview.

### 6.4 Mode toggle

A two-state toggle (`Adw.ToggleGroup`, or two linked `Gtk.ToggleButton`s on
older libadwaita): **Same for all** | **Per file**. The last choice is saved
as `processing.sort_mode` and pre-selected next time.

**Same for all** (`all_same`)

- Dropping any file(s) on a directory icon sends **every remaining file** in
  the grid to that directory and processes them all.

**Per file** (`per_file`)

- Only the dragged file(s) go to the directory where they are dropped and
  are processed; the others stay in the grid for the user to sort.

### 6.5 Drag-and-drop to a destination (starts OCR)

- Drag source: the thumbnail under the pointer; if it is part of the current
  selection, the whole selection is dragged. Dragging any file of a chain
  drags the whole chain.
- Drop target: each icon of the destination row (`Gtk.DropTarget`), which
  highlights on hover.
- Drag icon: a small document (Papirus `documentation.svg`, GPL-3, bundled
  in `flypage/ui/icons/`) with a badge giving the number of files, shown
  below-right of the pointer so it never hides the folder being targeted.
- Icon fallbacks: `flypage/ui/icons/hicolor/symbolic/apps/` holds Adwaita's
  `document-edit-symbolic` (pencil of entry rows) and `image-missing-symbolic`
  (LGPL-3 or CC-BY-SA-3.0, to list in debian/copyright), added to the icon
  search path after the user's theme, for themes such as MATE that lack them.
- On drop: the files get that destination and are sent **straight to the
  pipeline** (§7); there is no Launch button. They become greyed out with a
  per-thumbnail spinner, can no longer be selected, and are removed from the
  grid when their PDF is written. Failed files come back with an error badge.
- Clicking a directory icon does the same with the current selection
  (keyboard-accessible alternative to dragging).
- Dropping files on another thumbnail moves them before it (page order of
  a chain); dropping on the empty area after the last file moves them to
  the end.
- Several drops can be queued while OCR runs; the progress bar shows
  `pages done / total pages queued` and the current file name.
- When the grid is empty and the queue is done: a summary page (PDFs per
  folder, one *Open <folder>* button each, *Close*) with a 10-second
  countdown, and a desktop notification (no actions: the app has exited
  by the time one would be clicked).

### 6.6 Chain (merging)

- **Chain** button: links the selected files (2 or more) into one chain →
  a single PDF. Without a chain, each file gives its own PDF.
- Pages follow thumbnail order, which the user changes by drag-and-drop
  **inside the grid**.
- Several chains can exist (each gets its own colour and number badge).
  Pressing *Chain* on a selection that is already one chain unlinks it; a
  context menu on a thumbnail offers *Remove from chain*.
- A chain moves as one unit when dropped on a destination.
- Output name: built from the file name template (§5); the preview pane shows
  the resulting file name for the selected file/chain.

### 6.7 Buttons

- **Delete** (next to *Chain*, key *Delete*, or right-click menu):
  confirmation dialog, then deletes the selected file(s) **from the scanner
  memory** and from staging; thumbnails removed. (Milestone 4, `flypage sort
  DIR`: deletes them from DIR.) If the scanner was unplugged meanwhile, deletion is recorded in
  `~/.local/state/flypage/pending-deletions.json` and applied at next plug
  (matched by profile + relative path + size + mtime).
- **Cancel** (bottom) or closing the window: stops everything (including a
  running OCR, after a confirmation while OCR runs), removes staging and
  temporary files; nothing is deleted from the scanner. PDFs already
  written stay in their destination.
- Keyboard: *Ctrl+L* chain, *Delete* delete, *Ctrl+A* select all,
  *Enter*/double-click toggles the full-window preview.

### 6.8 Implementation (milestone 4)

- `flypage/session.py`: the window's logic without widgets (file list as a
  `Gio.ListStore`, chains, order, delete, send → pipeline queue, progress
  and end signals on the main thread); unit-tested.
- `flypage/thumbnails.py`: first-page renderings (Pillow; Poppler + cairo
  for PDFs) cached in `~/.cache/flypage/thumbs/`, rendered by 2 worker
  threads; only grid thumbnails stay in memory, previews are reloaded from
  the disk cache.
- `flypage/ui/processing_window.py`, `thumbnail_grid.py`,
  `destination_bar.py`, `style.css`.
- `flypage sort DIR [--keep-originals]` opens the window on the PNG/JPEG/PDF
  files under DIR (hidden folders such as `.Trash-1000` skipped). Each
  file is deleted once its PDF is written, unless `--keep-originals`.
- The preview shows the name the file or chain would get in *Default*.

---

## 7. Processing pipeline

For each **Document** (a single file or a chain):

1. **Normalise to PDF**: PNG/JPEG → `img2pdf` (EXIF rotation applied;
   transparent images flattened onto white first); PDF kept as is. The type
   is detected from the content, not the extension.
2. **Merge** (chains only): concatenate pages in user order with `pikepdf`.
3. **OCR**:
   - Tesseract: `ocrmypdf --skip-text -l eng+fra --output-type pdf in.pdf out.pdf`
     (recommended additional options in §7.1).
4. **Write** to `<destination>/<template-name>.pdf` (destination override or
   global file name template, §5); on name collision append `-1`, `-2`, …
5. **Remove originals**: delete the staging copies; if the profile has
   `delete_after_import = true`, delete the source files on the scanner
   (or queue as pending deletions if unplugged).
6. Errors on one document do not stop the others; failed documents keep
   their originals and are listed in the summary.

Implementation (`flypage/pipeline.py`):

- Documents are queued one by one with `Pipeline.submit()`, so each
  drag-and-drop in the window can add work while OCR runs. The output name
  (and its `%n`) is fixed at submit time, in drop order.
- A thread pool runs one document per CPU core; each runs
  `ocrmypdf --jobs 1`, so parallelism is across documents (scanner batches
  are mostly single pages). See §7.1 for counting physical cores rather
  than hardware threads.
- Progress (`pages done / pages total`) advances when a document finishes;
  the callback is called from worker threads, so the window wraps it in
  `GLib.idle_add`.
- OCR is written to a hidden `.flypage-*.pdf.part` file in the destination,
  then renamed: a half-written PDF is never visible. Intermediate files go
  to `~/.cache/flypage/work/` (not `/tmp`, which may be a small tmpfs).
- Cancel kills the OCR process groups; queued documents are skipped and
  reported as cancelled. The CLI cancels the same way on Ctrl+C or SIGTERM.

### 7.1 OCR engine choice and tuning (evaluated 2026-11-19)

Target computer: **Intel Core i5-3320M** (2012, 2 cores / 4 threads, AVX
but **no AVX2**), 16 GB RAM. Measurements were made on an i7-5600U (same
class, 2 cores / 4 threads) with 2 threads; EasyOCR was also run with AVX2
code paths disabled to estimate the i5-3320M. Test images were synthetic
French documents (DejaVu fonts, noise, skew); real IRIScan pages should
confirm the figures (see §11). The test harness is in
`../easyocr/compare/`.

**Tesseract vs EasyOCR 1.7.2** (Debian `python3-torch` 2.6, CPU). CER =
character error rate, lower is better. EasyOCR times are the old-CPU
estimate.

| Image | Tesseract CER | Tesseract time | EasyOCR CER | EasyOCR time |
| --- | --- | --- | --- | --- |
| Scanned A4 page | **0.0 %** | 0.8 s | 1.6 % | 47 s (15 s at canvas 1280) |
| Invoice / table | **0.0 %** (with `--psm 6`) | 0.3 s | 4.3 % | 17 s |
| Low contrast | **0.0 %** | 0.3 s | 3.5 % | 12 s |
| Small text | **0.6 %** | 0.2 s | 4.5 % | 5 s |
| Screenshot | 9.6 % | 0.3 s | **3.4 %** | 11 s |
| Phone photo of a page | **12.2 %** (after cleanup) | ≈1 s | 28.4 % | 26 s |
| Poster / sign | 76 % (fails) | 0.2 s | **1.6 %** | 9 s |

Conclusions:

- **Keep Tesseract as the only engine.** A document scanner produces flat,
  well-lit 300 dpi pages, which is exactly where Tesseract is best. EasyOCR
  would be slower by 10–60×, needs ≈1.5 GB of PyTorch, 1–3 GB of RAM per
  worker, and a custom package (EasyOCR 1.7.2 also needs a patch for
  Debian's `python3-bidi` 0.4: `from bidi.algorithm import get_display`).
- Tesseract's real errors in the test were in-word accent strings without
  a dictionary context (`àâæçéèêë…` alone): normal French words, `œ æ « » €
  ’` were all read correctly.

**ocrmypdf options on an IRIScan-sized page** (5120×6176 JPEG, 300 dpi,
1.2° skew, noise; one core, `OMP_THREAD_LIMIT=1`; CER ignores whitespace):

| Options | Time | CER |
| --- | --- | --- |
| current: `-l eng+fra` | 45.3 s | 0.27 % |
| `-l fra` | 38.7 s | 0.00 % |
| `-l fra --tesseract-downsample-above 3500` | 33.3 s | 0.00 % |
| `-l fra --tesseract-downsample-above 2600` | **31.5 s** | **0.00 %** |
| `-l eng+fra --tesseract-downsample-above 2600` | 34.1 s | 0.53 % |
| `-l fra --clean` (unpaper) | 49.5 s | 0.00 % |
| `-l fra --rotate-pages` | 46.0 s | 0.00 % |

Recommendations (to implement in milestone 10):

1. **Downsample before OCR**: add `--tesseract-downsample-above 2600`.
   Only the image sent to Tesseract is reduced; the image stored in the
   output PDF is unchanged. −30 % time, no accuracy loss: at 2600 px the
   text height is still well above what Tesseract needs.
2. **Use as few languages as possible.** Each extra language slows
   Tesseract and lets it pick the wrong dictionary (`eng+fra` was both the
   slowest and the least accurate). Keep the language list user-selectable
   (already done), but default to the GUI/system language only, e.g.
   `["fra"]` on a French desktop, and say in Settings that adding a
   language costs speed.
3. **Workers = physical cores**, not `os.cpu_count()` (4 on the i5-3320M,
   which has 2 cores): two Tesseract processes on the hyper-threads of one
   core compete for the same AVX units. Count unique `(physical id, core
   id)` pairs in `/proc/cpuinfo`, falling back to `os.cpu_count()`. Also
   pass `OMP_THREAD_LIMIT=1` in ocrmypdf's environment so Tesseract's own
   OpenMP threads do not oversubscribe the CPU.
4. **Optional, off by default** (Settings → OCR → *Advanced*):
   - `--rotate-pages` (+15 % time) for pages scanned upside down; needs
     `tesseract-ocr-osd`, which is already installed with `tesseract-ocr`.
   - `--clean` (+50 % time, needs `unpaper`): helps only on dirty or
     shadowed scans; it does not change the output image (unlike
     `--clean-final`).
   - Do **not** use `--deskew`: it re-encodes the page image (lossy) and the
     IRIScan JPEG data is already fragile (see §3, exit code 4).
5. **Tables (invoices)**: Tesseract's default page segmentation reads tables
   column by column (searchable, but copy-paste is out of order).
   `--tesseract-pagesegmode 6` reads them row by row (0 % CER on the test
   invoice) but is worse on multi-column text: offer it as a per-destination
   option (e.g. the *Invoices* destination), not as the global default.

**Improving the Tesseract model itself** (not planned; in order of cost):

1. *Better stock model*: `tessdata_best/fra.traineddata` (float LSTM,
   github.com/tesseract-ocr/tessdata_best) is slightly more accurate than
   Debian's `tesseract-ocr-fra`, but slower (not measured here). It can be tried without
   code changes by pointing `TESSDATA_PREFIX` to a folder containing it.
2. *User words / patterns*: `--user-words` (a list of domain words: names,
   suppliers, product names) and `--user-patterns` (e.g. invoice numbers
   `FA-\d\d\d\d-\d\d\d\d`) bias recognition at no speed cost. Could be a
   per-destination text file.
3. *Fine-tuning* with `tesstrain` from `fra` on the user's own scans:
   needs a few hundred to a few thousand corrected text lines, hours of CPU
   training, and care not to over-fit. Only worth it if real scans show
   systematic errors that 1–2 do not fix; the synthetic test shows ≈0 %
   CER on clean scans already.

---

## 8. Internationalisation

- All strings in English wrapped with `_()`; gettext domain `flypage`.
- First translation: `po/fr.po`.
- If `ui_language` ≠ `system`, set `LANGUAGE` before GTK initialises.
- `.desktop` and metainfo files translated via `i18n.merge_file`.

---

## 9. Debian package

`debian/control`:

- **Depends**: `python3 (>= 3.11), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1,
  gir1.2-poppler-0.18, python3-gi-cairo, python3-xlib, udisks2, udev, ocrmypdf,
  img2pdf, python3-pikepdf, python3-pil, python3-tomli-w, tesseract-ocr,
  tesseract-ocr-eng, tesseract-ocr-fra`
- **Recommends**: `libnotify-bin`, `iso-codes` (translated OCR language names)
- **Suggests**: `unpaper` (only if the optional *clean* setting of §7.1 is
  implemented). No `python3-torch`: EasyOCR is dropped.

Build: `dpkg-buildpackage -us -uc`, check with `lintian`, test install on a
clean Debian VM.

---

## 10. Milestones

1. ✅ **Skeleton** — Meson, package layout, config load/save with defaults,
   gettext; `flypage --version` runs from the source tree.
2. ✅ **Pipeline CLI** — `flypage process FILE... --dest DIR [--merge]` with
   Tesseract; unit tests with sample PNG / JPEG / PDF fixtures. `naming.py`
   and its tests were done here, since the pipeline names its output.
3. ✅ **Settings window** — destinations, OCR options, GUI language, file
   name template with live example (`%` codes, see §5).
4. ✅ **Processing window on a folder** — `flypage sort DIR`: thumbnail grid,
   preview, mode toggle, chains, drag-to-directory OCR + progress, delete, cancel.
5. ✅ **Device layer + daemon** — UDisks2 detection, scanner profiles and
   "plug now" wizard, rw mount, staging copy, delete on scanner, pending
   deletions, auto-launch of the processing window.
6. ✅ **Focus on open (X11, no keep-above)** and notifications.
7. ✖ **EasyOCR backend** — dropped after the 2026-11-19 evaluation (§7.1).
   Its engine choice is removed from Settings and from `ocr/__init__.py`;
   a config with `engine = "easyocr"` falls back to `tesseract` with a log
   message.
8. ✅ **French translation, icons, desktop/metainfo files** — app icon
   (`io.github.flypage`, scalable + symbolic, in `data/icons/hicolor`);
   `io.github.flypage.Settings.desktop` (menu entry), `…Processing.desktop`
   (hidden, matches the window's WM_CLASS), `…Daemon.desktop` (autostart),
   `io.github.flypage.metainfo.xml`; all translated through `po/` with
   `i18n.merge_file`, validated by `meson test` (desktop-file-validate,
   appstreamcli). The metainfo has no homepage URL yet.
9. **Debian packaging** and end-to-end test with the IRIScan Book.
10. **OCR tuning** (§7.1): `--tesseract-downsample-above 2600`; default
    OCR language = GUI/system language only; workers = physical cores +
    `OMP_THREAD_LIMIT=1`; optional *rotate pages*, *clean* and per-destination
    *table layout* (`--tesseract-pagesegmode 6`) settings. Validate on real
    IRIScan scans and on the i5-3320M before changing defaults.

---

## 11. Open questions

- The §7.1 figures come from synthetic pages. Run the same comparison on
  a few real IRIScan scans (with their corrected text) on the i5-3320M
  before changing defaults.
- Are most documents French only? If so, `["fra"]` is the better default;
  if many mix French and English, keep `eng+fra` and accept ≈15 % more time.
