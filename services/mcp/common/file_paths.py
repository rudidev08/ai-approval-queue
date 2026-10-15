"""file_paths — the path rules the files server, mail and the ops filing areas share.

Standard library only: mail's venv carries none of the files server's
packages. The tool-approvals plugin keeps its own copy of these rules with the
real paths written in (it imports nothing from services/mcp); its tests check
the copy against this module.

A path is judged as the host sees it: a /root/.hermes path (how the assistant's
sandbox sees ~/.hermes) maps to the host path first. Comparisons ignore letter
case, as macOS does.
"""

import os

from data_root import root

# The six folders the files tools work in, one variable each.
FOLDER_VARS = ("ASSISTANT_DESK", "ASSISTANT_PAPERS", "ASSISTANT_SHARED_PAPERS", "ASSISTANT_DEV",
               "ASSISTANT_DESKTOP", "ASSISTANT_DOWNLOADS")

# A top-level folder in Papers or SharedPapers that search and filing skip.
IGNORED = "ignore"

HOME = os.path.expanduser("~")
CONTAINER_HERMES = "/root/.hermes"
HOST_HERMES = os.path.join(HOME, ".hermes")

# Files people send the assistant and hermes' own working files: read-only sources.
# hermes owns them, so they keep fixed paths.
ATTACHMENT_FOLDERS = tuple(os.path.join(HOST_HERMES, p) for p in (
    "webui/attachments", "attachments", "images", "composer-pastes",
    "cache/documents", "cache/images", "cache/spillover"))


def folders(names=FOLDER_VARS):
    """The real paths of the named folders."""
    return [os.path.realpath(root(n)) for n in names]


def attachment_folders():
    return [os.path.realpath(p) for p in ATTACHMENT_FOLDERS]


def host_path(path):
    """path as the host sees it: absolute, with a /root/.hermes path mapped
    to ~/.hermes and ../ collapsed as text. ~ and relative paths raise
    ValueError: hermes expands ~ in the sandbox, where it is /root."""
    path = path or ""
    if not os.path.isabs(path):
        raise ValueError(f"{path!r} is not a full path — give the absolute path, "
                         "never ~ or a relative one")
    path = os.path.normpath(path)
    if path == CONTAINER_HERMES or path.startswith(CONTAINER_HERMES + os.sep):
        path = HOST_HERMES + path[len(CONTAINER_HERMES):]
    return path


def judged(path, follow=False):
    """The path the rules judge: fully resolved when follow, else the parent
    folder resolved and the last name kept, so a symbolic link is judged as
    the link itself."""
    path = host_path(path)
    if follow:
        return os.path.realpath(path)
    parent, name = os.path.split(path)
    return os.path.join(os.path.realpath(parent), name)


def inside(path, roots):
    """True when path is one of roots or inside one, ignoring case."""
    p = path.casefold()
    return any(p == r.casefold() or p.startswith(r.casefold() + os.sep) for r in roots)


def holds(path, protected):
    """True when path is one of protected or a folder holding one."""
    return any(inside(p, [path]) for p in protected)


def in_git(path):
    """True when path is a .git folder or inside one."""
    return any(part.casefold() == ".git" for part in path.split(os.sep))
