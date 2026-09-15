"""Select an explicit status-only preview before importing the voice SDK."""

import json
import os
import sys
import time


def main():
    if not os.environ.get("TZ"):
        os.environ["TZ"] = "America/Los_Angeles"
    if hasattr(time, "tzset"):
        time.tzset()
    if sys.argv[1:] in (["version"], ["--version"]):
        from .build_info import BUILD_INFO

        print(json.dumps(BUILD_INFO))
        return
    if sys.argv[1:2] == ["preview"]:
        from .preview import main as preview_main

        preview_main(sys.argv[2:])
        return
    from .worker import main as worker_main

    worker_main()
