"""Command-line entry point: ``flypage [--version] COMMAND``."""

import argparse
import logging
import sys

from flypage import cli, config, defs, i18n
from flypage.i18n import _


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flypage",
        description=_("Import documents from a scanner's memory and turn them into searchable PDFs."),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {defs.VERSION}")
    parser.add_argument("-v", "--verbose", action="store_true", help=_("show debug messages"))

    commands = parser.add_subparsers(dest="command", metavar=_("COMMAND"))
    commands.add_parser("settings", help=_("open the settings window"))
    commands.add_parser("daemon", help=_("watch for scanners being plugged in"))

    sort = commands.add_parser("sort", help=_("open the processing window on a folder of files"))
    sort.add_argument("directory", help=_("folder containing the files to sort"))
    sort.add_argument(
        "--keep-originals",
        action="store_true",
        help=_("keep the files once their PDF is created (by default they are deleted)"),
    )

    window = commands.add_parser("import-window", help=_("open the processing window on files imported by the daemon"))
    window.add_argument("staging", help=_("import folder created by the daemon"))

    process = commands.add_parser("process", help=_("convert files to searchable PDFs without a window"))
    process.add_argument("files", nargs="+", metavar=_("FILE"), help=_("PNG, JPEG or PDF file"))
    process.add_argument("--dest", required=True, metavar=_("DIR"), help=_("output folder"))
    process.add_argument("--merge", action="store_true", help=_("merge all files into one PDF"))
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="flypage: %(levelname)s: %(message)s")

    # The UI language comes from the settings, so they are read before any
    # translated string (including --help) is produced.
    cfg = config.load()
    i18n.setup(cfg.ui_language)

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "settings":
        from flypage.application import run_settings

        return run_settings()
    if args.command == "daemon":
        from flypage.daemon import run

        return run()
    if args.command == "import-window":
        return cli.import_window(args)
    if args.command == "sort":
        return cli.sort(args)
    if args.command == "process":
        try:
            return cli.process(args, cfg)
        except KeyboardInterrupt:  # before any OCR started: nothing to clean up
            return cli.EXIT_INTERRUPTED

    print(_("The '{command}' command is not implemented yet.").format(command=args.command), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
