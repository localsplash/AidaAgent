"""Embed source metadata in installed packages without importing the voice SDK."""

import json
import runpy
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def run(self):
        metadata = runpy.run_path(str(Path(__file__).with_name("build_metadata.py")))
        info = metadata["build_info"]()
        super().run()
        target = Path(self.build_lib) / "aida_agent" / "build-info.json"
        target.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


setup(cmdclass={"build_py": BuildPy})
