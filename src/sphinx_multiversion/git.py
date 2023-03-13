# -*- coding: utf-8 -*-
"""Helper functions for working with Git repositories"""

from collections import namedtuple
from collections.abc import Iterable
import datetime
import logging
import os
import re
import subprocess
import tarfile
import tempfile
from typing import Union

GitVersionRef = namedtuple(
    "GitVersionRef",
    [
        "name",
        "commit",
        "source",
        "is_remote",
        "refname",
        "creatordate",
    ],
)

logger = logging.getLogger(__name__)


def get_toplevel_path(cwd: Union[str, None] = None) -> str:
    """Execute Git command to get top level path"""
    cmd = (
        "git",
        "rev-parse",
        "--show-toplevel",
    )
    output = subprocess.check_output(cmd, cwd=cwd).decode()
    return output.rstrip("\n")


def get_all_refs(gitroot: str) -> Iterable[GitVersionRef]:
    """Execute Git command to get all references"""
    cmd = (
        "git",
        "for-each-ref",
        "--format",
        "%(objectname)\t%(refname)\t%(creatordate:iso)",
        "refs",
    )
    output = subprocess.check_output(cmd, cwd=gitroot).decode()
    for line in output.splitlines():
        is_remote = False
        fields = line.strip().split("\t")
        if len(fields) != 3:
            continue

        commit = fields[0]
        refname = fields[1]
        creatordate = datetime.datetime.strptime(fields[2], "%Y-%m-%d %H:%M:%S %z")

        # Parse refname
        matchobj = re.match(r"^refs/(heads|tags|remotes/[^/]+)/(\S+)$", refname)
        if not matchobj:
            continue
        source = matchobj.group(1)
        name = matchobj.group(2)

        if source.startswith("remotes/"):
            is_remote = True

        yield GitVersionRef(name, commit, source, is_remote, refname, creatordate)


def get_refs(
    gitroot: str, tag_whitelist: str, branch_whitelist: str, remote_whitelist: str, files: tuple[str, ...] = ()
) -> Iterable[GitVersionRef]:
    """Filter Git references"""
    for ref in get_all_refs(gitroot):
        if ref.source == "tags":
            if tag_whitelist is None or not re.match(tag_whitelist, ref.name):
                logger.debug(
                    "Skipping '%s' because tag '%s' doesn't match the whitelist pattern",
                    ref.refname,
                    ref.name,
                )
                continue
        elif ref.source == "heads":
            if branch_whitelist is None or not re.match(branch_whitelist, ref.name):
                logger.debug(
                    "Skipping '%s' because branch '%s' doesn't match the whitelist pattern",
                    ref.refname,
                    ref.name,
                )
                continue
        elif ref.is_remote and remote_whitelist is not None:
            remote_name = ref.source.partition("/")[2]
            if not re.match(remote_whitelist, remote_name):
                logger.debug(
                    "Skipping '%s' because remote '%s' doesn't match the whitelist pattern",
                    ref.refname,
                    remote_name,
                )
                continue
            if branch_whitelist is None or not re.match(branch_whitelist, ref.name):
                logger.debug(
                    "Skipping '%s' because branch '%s' doesn't match the whitelist pattern",
                    ref.refname,
                    ref.name,
                )
                continue
        else:
            logger.debug("Skipping '%s' because its not a branch or tag", ref.refname)
            continue

        missing_files = [
            filename for filename in files if filename != "." and not file_exists(gitroot, ref.refname, filename)
        ]
        if missing_files:
            logger.debug(
                "Skipping '%s' because it lacks required files: %r",
                ref.refname,
                missing_files,
            )
            continue

        yield ref


def file_exists(gitroot: str, refname: str, filename: str) -> bool:
    """Execute Git command to check if file exists"""
    if os.sep != "/":
        # Git requires / path sep, make sure we use that
        filename = filename.replace(os.sep, "/")

    cmd = (
        "git",
        "cat-file",
        "-e",
        f"{refname}:{filename}",
    )
    proc = subprocess.run(cmd, cwd=gitroot, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return proc.returncode == 0


def copy_tree(gitroot: str, dst: str, reference: GitVersionRef, sourcepath: str = ".") -> None:
    """Execute Git command to copy repository tree"""
    with tempfile.SpooledTemporaryFile() as fp:
        cmd = (
            "git",
            "archive",
            "--format",
            "tar",
            reference.commit,
            "--",
            sourcepath,
        )
        subprocess.check_call(cmd, cwd=gitroot, stdout=fp)
        fp.seek(0)
        with tarfile.TarFile(fileobj=fp) as tarfp:
            tarfp.extractall(dst)
