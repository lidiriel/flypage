"""Gettext setup.

Modules translate strings with ``from flypage.i18n import _``. ``setup()`` must
run once at start-up, before GTK is initialised, so that a forced UI language
also applies to GTK's own strings.
"""

import gettext
import locale
import logging
import os

from flypage import defs

DOMAIN = "flypage"
SUPPORTED_UI_LANGUAGES = ("system", "en", "fr")

log = logging.getLogger(__name__)

_ = gettext.gettext
ngettext = gettext.ngettext


def N_(message: str) -> str:
    """Mark a string for translation without translating it yet (translate with _() when shown)."""
    return message


def setup(ui_language: str = "system", localedir: str | None = None) -> None:
    """Bind the text domain and apply the UI language from the settings."""
    if ui_language != "system":
        os.environ["LANGUAGE"] = ui_language

    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error as err:
        log.debug("cannot set locale: %s", err)

    localedir = localedir or defs.LOCALEDIR
    gettext.bindtextdomain(DOMAIN, localedir)
    gettext.textdomain(DOMAIN)
    # GtkBuilder translates .ui files through the C library's gettext.
    if hasattr(locale, "bindtextdomain"):
        locale.bindtextdomain(DOMAIN, localedir)
        locale.textdomain(DOMAIN)
