# Flypage

Flypage imports the documents stored in a scanner's internal memory (for
example an IRIScan Book) and turns them into **searchable PDFs**: the
scanned image with an invisible text layer from OCR behind it, so the text
can be searched, selected and copied.

## Goal

Some portable scanners save their scans on built-in memory and show up as
a USB disk when they are plugged in. Getting the files off, sorting them
into folders and running OCR on them by hand takes time. With Flypage:

1. You plug in the scanner. A small background service notices it, copies
   its files to a staging folder and opens the **processing window**.
2. You sort the scans by dragging them onto a destination folder
   (*Invoices*, *Medical*…), or onto *Other Folder…* to pick any folder.
   Several files dropped together still give one PDF each. A right-click
   deletes the ones you don't want.
3. Each file dropped on a folder is converted to PDF and sent through
   OCR right away. A progress bar shows the pages done and the time left,
   and each file being read shows its own progress ring (see
   [Progress](#progress)).
4. In *Move* mode (the default), once a PDF is written its original is
   deleted from the scanner's memory; in *Copy* mode it stays there. A
   notification lists the files made, with buttons that open their folders.

The toolbar at the bottom of the window sets what the next drop does:
*Images to PDF* (off: JPG and PNG files are filed as they are, without
OCR, since OCR only works on PDF), *Move* or *Copy*, and the OCR language
(the first one in Settings, another one, or all of them).

Input files can be PNG, JPEG or PDF. The output is a PDF with a text
layer (or the image itself when *Images to PDF* is off), named from a
template you choose (e.g. `2026-11-21_Invoices_001.pdf`). A **digital PDF**,
one that already contains text (made by office software, or already
OCR'd), is filed as it is, without OCR: the preview says so.

Everything else is set in the **Settings** window, opened from the system
menu: scanner profiles (several scanners are possible), destination
folders with their icons, the default destination, OCR languages (in
order: the first one is the window's default), optional OCR steps (see
[OCR speed and options](#ocr-speed-and-options)), the file name template
and the interface language. The interface is in English, with a French
translation.

OCR is done by [Tesseract](https://github.com/tesseract-ocr/tesseract)
through [ocrmypdf](https://ocrmypdf.readthedocs.io/). EasyOCR was
evaluated and dropped: on scanned documents Tesseract is as accurate or
better and 10–60× faster.

Flypage runs as the logged-in user and needs no root rights. It targets
Debian with a GTK 4 desktop.

## OCR speed and options

A full IRIScan page takes roughly 10–35 s of OCR, depending on the
computer. Flypage is tuned for that:

- Tesseract reads a copy of the page reduced to 2600 px
  (`--tesseract-downsample-above 2600`); the PDF keeps the full image.
  About 30 % faster with the same accuracy.
- One document per **physical core** (not per hardware thread), each
  Tesseract limited to one thread (`OMP_THREAD_LIMIT=1`).
- A new configuration starts with the desktop's language only. Every
  extra language used together slows OCR down and can make it pick the
  wrong dictionary. The window's language list still offers the others,
  and *All Languages*.

Off by default, because they cost time:

- **Straighten rotated pages** (Settings → OCR → Advanced; about 15 % more
  time; needs the `osd` model of `tesseract-ocr-osd`).
- **Clean pages before OCR** (same place; about 50 % more time; needs
  `unpaper`). The image saved in the PDF is not changed.
- **Tables**, per destination (e.g. *Invoices*): Tesseract reads tables row
  by row (`--tesseract-pagesegmode 6`), so copied text keeps the rows
  together. Worse on text laid out in columns.

## Progress

ocrmypdf only reports progress page by page, and a scan is usually one
page, so the bar would otherwise sit empty and then jump to full. Flypage
estimates instead: it measures how many seconds a page takes on this
computer (a running average saved in `~/.local/state/flypage/ocr-speed.json`)
and moves the bar by elapsed time against that estimate. A file being read
never shows 100 % before it is really done. The bar reads e.g.
*2 / 5 pages · about 1 min left · 2026-11-22_Invoices_003.pdf*. In the grid,
each queued file shows *Waiting* or a ring with its own percentage. The
first estimate (20 s per page) is a guess. After a few documents it
matches the computer.

## How it works

```text
 scanner plugged in
        │  UDisks2 (D-Bus) + udevadm: USB vendor/model IDs
        ▼
 flypage daemon ──► matches a scanner profile ──► mounts it ──► copies files to
 (autostart)                                                   ~/.cache/flypage/incoming/
        │
        ▼
 flypage import-window ──► thumbnail grid, drag to a destination
        │
        ▼
 pipeline: image → PDF (img2pdf) → OCR (ocrmypdf + Tesseract), one PDF per file
        │
        ▼
 PDF in the destination folder ──► original deleted on the scanner (now, or at the next plug)
```

## Code structure

Flypage is written in Python 3 with GTK 4 and libadwaita (PyGObject) and
built with Meson.

```text
flypage/
├── bin/flypage.in            launcher script, configured by Meson
├── flypage/                  Python package
│   ├── __main__.py           command line: settings | daemon | sort | import-window | process
│   ├── application.py        GTK applications (kept apart so --version does not load GTK)
│   ├── cli.py                commands that run without a window (process)
│   ├── config.py             load/save ~/.config/flypage/config.toml, defaults, validation
│   ├── paths.py              per-user XDG directories
│   ├── i18n.py, languages.py gettext setup; readable names for OCR language codes
│   │
│   ├── daemon.py             watches for scanners being plugged in, starts the import
│   ├── device.py             USB disks through UDisks2 (mount, unmount) and udev (USB IDs)
│   ├── scanners.py           which scanner profile a plugged USB disk matches
│   ├── importer.py           copies a scanner's files to staging, deletes them on the scanner
│   ├── pending.py            deletions kept for a scanner that was unplugged too early
│   │
│   ├── session.py            what the processing window does, without the widgets
│   ├── progress.py           OCR speed learned per computer, estimated progress and time left
│   ├── model.py              objects shown in the thumbnail grid
│   ├── thumbnails.py         first-page thumbnails, cached on disk
│   ├── naming.py             output file names from a template (%Y-%m-%d_%{dest}_%3n)
│   ├── pipeline.py           image → PDF, merging, OCR in parallel workers, progress, cancel
│   ├── cancel.py             cancellation shared by the pipeline and OCR
│   ├── notifications.py      desktop notifications and the window → daemon report
│   ├── ocr/
│   │   ├── base.py           interface an OCR engine implements
│   │   └── tesseract.py      Tesseract through ocrmypdf
│   └── ui/
│       ├── settings_window.py    settings; every valid change is saved at once
│       ├── processing_window.py  sort scans into folders, OCR progress
│       ├── thumbnail_grid.py     grid with drag-and-drop, multi-selection
│       ├── destination_bar.py    row of destination icons to drop scans on
│       ├── template_editor.py    file name template field with a live preview
│       ├── dialogs.py            add/edit destination and scanner profile
│       ├── activation.py         brings a new window to the front (X11)
│       ├── widgets.py, style.css, icons/
├── data/                     .desktop files (menu entry, processing window, autostart),
│                             AppStream metainfo, application icons
├── po/                       translations: flypage.pot, fr.po
├── tests/                    pytest suite
├── PLAN.md                   design notes, decisions and milestones
└── VERSION
```

The design behind each part (detection, configuration, windows, pipeline,
OCR measurements) is written down in [PLAN.md](PLAN.md).

## Build

### Dependencies

On Debian:

```sh
# to build
sudo apt install meson gettext desktop-file-utils appstream

# to run
sudo apt install python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-poppler-0.18 python3-xlib python3-pikepdf python3-pil python3-tomli-w \
    img2pdf ocrmypdf tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra udisks2

# to run the tests
sudo apt install python3-pytest
```

Add a `tesseract-ocr-<lang>` package for each other language your
documents use, e.g. `tesseract-ocr-deu` for German.

### Build and install

```sh
meson setup builddir --prefix=/usr
meson compile -C builddir
meson test -C builddir
sudo meson install -C builddir
```

This installs the `flypage` command, the **Flypage** settings entry in
the system menu, and an autostart entry that runs `flypage daemon` at
every login. Log out and back in (or run `flypage daemon`) and plug in the
scanner.

### Debian package

```sh
sudo apt install dpkg-dev debhelper dh-python lintian
dpkg-checkbuilddeps                 # lists any missing build dependency
dpkg-buildpackage -us -uc -b        # builds ../flypage_<version>_all.deb
lintian ../flypage_*.changes
sudo apt install ../flypage_*_all.deb
```

The package is native (`3.0 (native)`): the version in `debian/changelog`
must match `VERSION`. The tests run during the build, without a display
(the window tests are skipped); `DEB_BUILD_OPTIONS=nocheck` skips them.

## Development

Run from the source tree (uses the system Python, which has PyGObject):

```sh
/usr/bin/python3 -m flypage --version
/usr/bin/python3 -m flypage settings
/usr/bin/python3 -m flypage sort ~/some/folder --keep-originals
/usr/bin/python3 -m flypage -v daemon   # imports when a configured scanner is plugged in
/usr/bin/python3 -m flypage process scan.jpg --dest ~/Documents/Scans
```

Build, test and install with Meson:

```sh
meson setup builddir --prefix=/usr
meson compile -C builddir
meson test -C builddir          # needs python3-pytest and python3-tomli-w;
                                # window tests need a display (skipped without)
meson compile -C builddir flypage-pot      # refresh po/flypage.pot
meson compile -C builddir flypage-update-po  # merge it into po/*.po
sudo meson install -C builddir
```

The tests can also be run directly with `/usr/bin/python3 -m pytest tests`.
When you add a source file with translatable strings, list it in
`po/POTFILES.in`.

## Improvement

Ways to make OCR more accurate, from cheapest to most expensive:

### A better ready-made model

`tessdata_best/fra.traineddata` (from
github.com/tesseract-ocr/tessdata_best) is a little more accurate than
Debian's model, but slower. You can try it without touching any code: put
it in a folder and point `TESSDATA_PREFIX` there. Tesseract then looks
**only** in that folder, so also put there every other model you use
(e.g. `eng`), `osd` if *Straighten rotated pages* is on, and a link to
Tesseract's `configs` folder, without which OCR fails
(`ln -s /usr/share/tesseract-ocr/5/tessdata/configs ~/.tesseract-ocr/`). The
variable must be set where Flypage starts: in `~/.profile` (then log in
again) for the daemon and the menu entry, not only in a terminal.

### Word lists and patterns

`--user-words` takes a list of your own vocabulary: names, suppliers,
products. `--user-patterns` takes formats such as `FA-\d\d\d\d-\d\d\d\d`.
They improve recognition at no speed cost, and could be one file per
destination.

### Retraining on your own scans

Use tesstrain, starting from the `fra` model. This needs a few hundred to
a few thousand corrected text lines and hours of CPU time. It's only worth
doing if real scans show the same errors again and again after the two
options above.

On clean scans Tesseract already scored about 0 % errors. In practice,
image quality and the options above matter more than the model.
