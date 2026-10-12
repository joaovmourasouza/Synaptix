import gettext
import locale
import os

DOMAIN = "synaptix"


def setup(localedir: str):
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if os.path.isdir(localedir):
        locale.bindtextdomain(DOMAIN, localedir)
        gettext.bindtextdomain(DOMAIN, localedir)
    locale.textdomain(DOMAIN)
    gettext.textdomain(DOMAIN)


def _(message: str) -> str:
    return gettext.dgettext(DOMAIN, message)
