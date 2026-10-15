"""A stored file's name ends in its source file's extension: the files server
adds it to a copied or moved file's name, and the actions page shows the name
it will store."""

import mimetypes
import os

FILENAME_CAP = 200  # the longest name the files server stores


def file_ext(name):
    """name's extension, or '' when the part after its last dot holds a
    space — the dot in a name like 'Invoice No. 5' starts no extension."""
    ext = os.path.splitext(name)[1]
    return "" if " " in ext else ext


def stored_name(name, source):
    """name with source's extension added, or name as it is when it already
    ends in that extension, in another spelling of its type ('.jpeg' for
    '.jpg'), or source has none. Nothing is cut off: a dot in 'Receipt
    Amazon.com' can look like one."""
    ext = file_ext(source)
    if not ext or name.lower().endswith(ext.lower()):
        return name
    kind = mimetypes.guess_type("x" + ext)[0]
    if kind and kind == mimetypes.guess_type("x" + file_ext(name))[0]:
        return name
    return name + ext
