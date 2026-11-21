

Flypage is an application to transfert automatically all document from a scan to a computer and convert it in text

# features

- convert image to PDF
- move scanner files to predefined directory on the computer
- apply to the pdf an OCR process and add a layer with text detected behind image

# needs

- debian packaged
- user parameters
  - configuration of the specifica scan with memory (several scan are possible, like IRIScan Book)
  - OCR choice tesserac or easyocr
  - choice several language for OCR and one for GUI
  - list of proposed directory : user can add several named directory + icon
  - set a default directory (@see GUI explanation)
- a service system for detect scan plug/unplug
- a GUI for settings launch with system menu entry
- a GUI for processing, launch when scan is plug
- default first language : english message with in first translation : french 
- entry supported files : PNG, JPEG, PDF
- ouput : PDF with a text layer

# GUI processing

- very simple GUI launch on top of other system windows
- no menu bar, only message on the top, with icon
- first step scan detection
- scan exploration : select all file
- ask user for each file : where the file must be push (one icon by directories) + default directory on bottom.
- in the center of the GUI bellow message, put a view of the image
- add a cancel button to cancel all operation, a delete button to delete file, and a launch button to process 
- add a progression bar for OCR operation
 





# development

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
# improvment

From cheapest to most expensive:

## A better ready-made model. 
tessdata_best/fra.traineddata (from github.com/tesseract-ocr/tessdata_best) is a little more accurate than Debian's model, but slower. You can try it without touching any code: put it in a folder and point TESSDATA_PREFIX there.

## Word lists and patterns. 
--user-words takes a list of your own vocabulary: names, suppliers, products. --user-patterns takes formats such as FA-\d\d\d\d-\d\d\d\d. They improve recognition at no speed cost, and could be one file per destination.

## Retraining on your own scans with tesstrain, starting from the fra model. 
This needs a few hundred to a few thousand corrected text lines and hours of CPU time. It's only worth doing if real scans show the same errors again and again after options 1 and 2.

On clean scans Tesseract already scored about 0 %. In practice, image quality and the options above matter more than the model.