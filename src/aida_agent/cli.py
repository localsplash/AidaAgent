"""Select an explicit status-only preview before importing the voice SDK."""

import sys


def main():
    if sys.argv[1:2] == ["preview"]:
        from .preview import main as preview_main

        preview_main(sys.argv[2:])
        return
    from .worker import main as worker_main

    worker_main()
