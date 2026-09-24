"""Apply PlatformConfig, then select the status-only preview or the voice worker."""

import json
import os
import sys
import time


def apply_platform_config(argv):
    """Settings come from the store before anything reads the environment.

    Help and version need no settings. The worker cannot start without them; the preview
    reports whatever is missing instead, since that is what it exists to show.
    """
    if "--help" in argv or "-h" in argv:
        return
    from .platform_config import SettingsUnavailable, apply

    try:
        keys = apply()
    except SettingsUnavailable as error:
        if argv[:1] == ["preview"]:
            print(f"[settings] PlatformConfig unavailable, reporting the environment as is: {error}",
                  file=sys.stderr, flush=True)
            return
        raise SystemExit(f"[settings] Cannot start: {error}") from None
    print(f"[settings] PlatformConfig applied: {', '.join(keys) or 'no rows'}", flush=True)


def main():
    if not os.environ.get("TZ"):
        os.environ["TZ"] = "America/Los_Angeles"
    if hasattr(time, "tzset"):
        time.tzset()
    if sys.argv[1:] in (["version"], ["--version"]):
        from .build_info import BUILD_INFO

        print(json.dumps(BUILD_INFO))
        return
    apply_platform_config(sys.argv[1:])
    if sys.argv[1:2] == ["preview"]:
        from .preview import main as preview_main

        preview_main(sys.argv[2:])
        return
    from .worker import main as worker_main

    worker_main()
