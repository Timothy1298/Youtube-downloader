import sys


def main() -> int:
    from youtube_downloader.main import main as pkg_main  # type: ignore

    # If no CLI args supplied, launch GUI by default
    argv = ["--gui"] if len(sys.argv) <= 1 else sys.argv[1:]
    return pkg_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())


