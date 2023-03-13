# -*- coding: utf-8 -*-
"""Command line interface for building multiversion sphinx documentation"""

import argparse
import contextlib
import datetime
import itertools
import json
import logging
import multiprocessing
import os
import pathlib
import re
import shutil
import string
import subprocess
import sys
import tempfile

from sphinx import config as sphinx_config
from sphinx import project as sphinx_project

from . import git, sphinx


@contextlib.contextmanager
def _set_working_dir(path):
    """Change current working directory temporary, e.g. within a context manager"""
    prev_cwd = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev_cwd)


def _create_sphinx_config_worker(q, confpath, confoverrides, add_defaults):
    """Create a worker to load sphinx configuration"""
    try:
        with _set_working_dir(confpath):
            current_config = sphinx_config.Config.read(
                confpath,
                confoverrides,
            )

        if add_defaults:
            current_config.add("smv_tag_whitelist", sphinx.DEFAULT_TAG_WHITELIST, "html", str)
            current_config.add(
                "smv_branch_whitelist",
                sphinx.DEFAULT_TAG_WHITELIST,
                "html",
                str,
            )
            current_config.add(
                "smv_remote_whitelist",
                sphinx.DEFAULT_REMOTE_WHITELIST,
                "html",
                str,
            )
            current_config.add(
                "smv_released_pattern",
                sphinx.DEFAULT_RELEASED_PATTERN,
                "html",
                str,
            )
            current_config.add(
                "smv_outputdir_format",
                sphinx.DEFAULT_OUTPUTDIR_FORMAT,
                "html",
                str,
            )
            current_config.add("smv_prefer_remote_refs", False, "html", bool)
            current_config.add("smv_symver_pattern", r"^[^\d]*(\d*\.\d*).*$", "html", str)
        current_config.pre_init_values()
        current_config.init_values()
    except Exception as e:  # pylint: disable=broad-except
        q.put(e)
        return

    q.put(current_config)


def _load_sphinx_config(confpath, confoverrides, add_defaults=False):
    """Load sphinx config"""
    q = multiprocessing.Queue()
    proc = multiprocessing.Process(
        target=_create_sphinx_config_worker,
        args=(q, confpath, confoverrides, add_defaults),
    )
    proc.start()
    proc.join()
    result = q.get_nowait()
    if isinstance(result, Exception):
        raise result
    return result


def _get_python_flags():  # pylint: disable=too-many-branches
    """Get Python runtime flags that were provided through command line arguments or environment vars"""
    if sys.flags.bytes_warning:
        yield "-b"
    if sys.flags.debug:
        yield "-d"
    if sys.flags.hash_randomization:
        yield "-R"
    if sys.flags.ignore_environment:
        yield "-E"
    if sys.flags.inspect:
        yield "-i"
    if sys.flags.isolated:
        yield "-I"
    if sys.flags.no_site:
        yield "-S"
    if sys.flags.no_user_site:
        yield "-s"
    if sys.flags.optimize:
        yield "-O"
    if sys.flags.quiet:
        yield "-q"
    if sys.flags.verbose:
        yield "-v"

    for option, value in sys._xoptions.items():
        if value is True:
            yield from ("-X", option)
        else:
            yield from ("-X", f"{option}={value}")


def _generate_html_redirection_page(path=""):
    """Generate markup for HTML page which redirects to the latest released docs"""
    return fr'''<!-- This page is created automatically by documentation builder -->
<!DOCTYPE html>
<html>
  <head>
    <title>Redirecting to main branch docs</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="0; url=./{path}">
    <link rel="canonical" href="./{path}">
  </head>
</html>'''


def _create_argument_parser():
    """Create parser with custom arguments"""
    parser = argparse.ArgumentParser()
    parser.add_argument("sourcedir", help="path to documentation source files")
    parser.add_argument("outputdir", help="path to output directory")
    parser.add_argument(
        "filenames",
        nargs="*",
        help="a list of specific files to rebuild. Ignored if -a is specified",
    )
    parser.add_argument(
        "-c",
        metavar="PATH",
        dest="confdir",
        help=("path where configuration file (conf.py) is located " "(default: same as SOURCEDIR)"),
    )
    parser.add_argument(
        "-C",
        action="store_true",
        dest="noconfig",
        help="use no config file at all, only -D options",
    )
    parser.add_argument(
        "-D",
        metavar="setting=value",
        action="append",
        dest="define",
        default=[],
        help="override a setting in configuration file",
    )
    parser.add_argument(
        "--dump-metadata",
        action="store_true",
        help="dump generated metadata and exit",
    )
    parser.add_argument(
        "--skip-if-outputdir-exists",
        action="store_true",
        help="skip building version if its output directory exists",
    )
    parser.add_argument(
        "--dev-name",
        metavar="DEV_NAME",
        dest="dev_name",
        help=("name for the development version of docs to be built"),
    )
    parser.add_argument(
        "--dev-path",
        metavar="DEV_PATH",
        dest="dev_path",
        help=("path where to store the development version of docs " "(default: root build directory)"),
    )

    return parser


def _update_static_path(output_dir):
    """Change (in-place) path of _static folder in all HTML and CSS files of
    older versions of documentation to the _static folder of dev version, then
    remove the local _static folder. This allows to use a single copy of static
    files across all versions of documentation"""

    for root, _, files in os.walk(output_dir):
        for file in files:
            if file.endswith(".html") or file.endswith(".css"):
                file_path = os.path.join(root, file)
                with open(file_path, mode="r", encoding="utf-8") as f:
                    filedata = f.read()
                # Use relative path to the _static folder of dev version
                filedata = filedata.replace("_static", "../../_static")
                with open(file_path, mode="w", encoding="utf-8") as f:
                    f.write(filedata)

    # Remove the _static folder
    shutil.rmtree(os.path.join(output_dir, "_static"))


def main(argv=None):  # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    """Command line interface for building multiversion sphinx documentation"""
    if not argv:
        argv = sys.argv[1:]

    parser = _create_argument_parser()
    args, argv = parser.parse_known_args(argv)
    if args.noconfig:
        return 1

    logger = logging.getLogger(__name__)

    sourcedir_absolute = os.path.abspath(args.sourcedir)
    confdir_absolute = os.path.abspath(args.confdir) if args.confdir is not None else sourcedir_absolute

    # Conf-overrides
    confoverrides = {}
    for d in args.define:
        key, _, value = d.partition("=")
        confoverrides[key] = value

    # Parse config
    config = _load_sphinx_config(confdir_absolute, confoverrides, add_defaults=True)

    # Get relative paths to root of git repository
    gitroot = pathlib.Path(git.get_toplevel_path(cwd=sourcedir_absolute)).resolve()
    cwd_absolute = os.path.abspath(".")
    cwd_relative = os.path.relpath(cwd_absolute, str(gitroot))

    logger.debug("Git toplevel path: %s", str(gitroot))
    sourcedir = os.path.relpath(sourcedir_absolute, str(gitroot))
    logger.debug("Source dir (relative to git toplevel path): %s", str(sourcedir))
    if args.confdir:
        confdir = os.path.relpath(confdir_absolute, str(gitroot))
    else:
        confdir = sourcedir
    logger.debug("Conf dir (relative to git toplevel path): %s", str(confdir))
    conffile = os.path.join(confdir, "conf.py")

    # Get git references
    gitrefs = git.get_refs(
        str(gitroot),
        config.smv_tag_whitelist,
        config.smv_branch_whitelist,
        config.smv_remote_whitelist,
        files=(sourcedir, conffile),
    )

    # Order git refs
    if config.smv_prefer_remote_refs:
        gitrefs = sorted(gitrefs, key=lambda x: (not x.is_remote, *x))
    else:
        gitrefs = sorted(gitrefs, key=lambda x: (x.is_remote, *x))

    # git refs by default are just strings, and we need to extract symver to be able to reasonably sort versions
    gitrefs = sorted(gitrefs, key=lambda x: float(re.match(config.smv_symver_pattern, x.refname).group(1)))

    logger = logging.getLogger(__name__)
    released_versions = []

    with (tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as doctree_cache):
        # Generate Metadata
        metadata = {}
        outputdirs = set()
        for gitref in gitrefs:
            # Clone Git repo
            repopath = os.path.join(tmp, gitref.commit)
            try:
                git.copy_tree(str(gitroot), repopath, gitref)
            except (OSError, subprocess.CalledProcessError):
                logger.error(
                    "Failed to copy git tree for %s to %s",
                    gitref.refname,
                    repopath,
                )
                continue

            # Find config
            confpath = os.path.join(repopath, confdir)
            try:
                current_config = _load_sphinx_config(confpath, confoverrides)
            except (OSError, sphinx_config.ConfigError):
                logger.error(
                    "Failed load config for %s from %s",
                    gitref.refname,
                    confpath,
                )
                continue

            # Ensure that there are not duplicate output dirs
            outputdir = config.smv_outputdir_format.format(
                ref=gitref,
                config=current_config,
            )
            if outputdir in outputdirs:
                logger.warning(
                    "outputdir '%s' for %s conflicts with other versions",
                    outputdir,
                    gitref.refname,
                )
                continue
            outputdirs.add(outputdir)

            # Get List of files
            source_suffixes = current_config.source_suffix
            if isinstance(source_suffixes, str):
                source_suffixes = [current_config.source_suffix]

            current_sourcedir = os.path.join(repopath, sourcedir)
            project = sphinx_project.Project(current_sourcedir, source_suffixes)
            metadata[gitref.name] = {
                "name": gitref.name,
                "version": current_config.version,
                "release": gitref.name,
                "rst_prolog": current_config.rst_prolog,
                "is_released": bool(re.match(config.smv_released_pattern, gitref.refname)),
                "source": gitref.source,
                "creatordate": gitref.creatordate.strftime(sphinx.DATE_FMT),
                "basedir": repopath,
                "sourcedir": current_sourcedir,
                "outputdir": os.path.join(os.path.abspath(args.outputdir), outputdir),
                "confdir": confpath,
                "docnames": list(project.discover()),
            }

            if metadata[gitref.name]["is_released"]:
                released_versions.append(gitref.name)

        if args.dev_name:
            metadata[args.dev_name] = {
                "name": args.dev_name,
                "version": args.dev_name,
                "release": args.dev_name,
                "rst_prolog": current_config.rst_prolog,
                "is_released": False,
                "source": "heads",
                "creatordate": datetime.datetime.now(datetime.timezone.utc).strftime(sphinx.DATE_FMT),
                "basedir": repopath,
                "sourcedir": confdir_absolute,
                "outputdir": os.path.join(os.path.abspath(args.outputdir), args.dev_path or ""),
                "confdir": confpath,
                "docnames": list(project.discover()),
            }

        if args.dump_metadata:
            print(json.dumps(metadata, indent=2))
            return 0

        if not metadata:
            logger.error("No matching refs found!")
            return 2

        # Generate HTML page which redirects to latest released docs
        html_file_path = os.path.abspath(os.path.join(sourcedir, "_static/index.html"))
        with open(html_file_path, mode="w", encoding="utf-8") as fp:
            redirection_path = released_versions[-1] + "/index.html"
            fp.write(_generate_html_redirection_page(redirection_path))

        # Write Metadata
        metadata_path = os.path.abspath(os.path.join(tmp, "versions.json"))
        with open(metadata_path, mode="w", encoding="utf-8") as fp:
            json.dump(metadata, fp, indent=2)

        # Run Sphinx
        argv.extend(["-D", f"smv_metadata_path={metadata_path}"])
        for version_name, data in metadata.items():
            # When --skip-if-outputdir-exists flag passed, do not build version if its output
            # directory already exists. This does not check the contents of directory.
            if args.skip_if_outputdir_exists:
                if os.path.isdir(data["outputdir"]) and data["name"] != args.dev_name:
                    logger.warning(
                        "Skipping version because outputdir '%s' for %s already exists",
                        data["outputdir"],
                        data["name"],
                    )
                    continue

            os.makedirs(data["outputdir"], exist_ok=True)

            defines = itertools.chain(*(("-D", string.Template(d).safe_substitute(data)) for d in args.define))

            current_argv = argv.copy()
            current_argv.extend(
                [
                    *defines,
                    "-D",
                    f"smv_latest_version={released_versions[-1]}",
                    "-D",
                    f"smv_current_version={version_name}",
                    "-c",
                    confdir_absolute,
                    data["sourcedir"],
                    data["outputdir"],
                    *args.filenames,
                ]
            )
            logger.debug("Running sphinx-build with args: %r", current_argv)
            cmd = (
                sys.executable,
                *_get_python_flags(),
                "-m",
                "sphinx",
                "-d",
                doctree_cache,
                *current_argv,
            )
            current_cwd = os.path.join(data["basedir"], cwd_relative)
            env = os.environ.copy()
            env.update(
                {
                    "SPHINX_MULTIVERSION_NAME": data["name"],
                    "SPHINX_MULTIVERSION_VERSION": data["version"],
                    "SPHINX_MULTIVERSION_RELEASE": data["release"],
                    "SPHINX_MULTIVERSION_SOURCEDIR": data["sourcedir"],
                    "SPHINX_MULTIVERSION_OUTPUTDIR": data["outputdir"],
                    "SPHINX_MULTIVERSION_CONFDIR": data["confdir"],
                }
            )
            subprocess.check_call(cmd, cwd=current_cwd, env=env)

            # Use "master" copy of static files for all documentation releases
            if args.dev_name and (version_name != args.dev_name):
                _update_static_path(data["outputdir"])

    return 0
