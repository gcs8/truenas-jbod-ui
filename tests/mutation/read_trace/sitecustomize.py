"""Record every file a Python process opens, for the topology mutation runs (#1516).

The mutation runners put this directory on PYTHONPATH for the unmutated
run only, with TOPOLOGY_READ_TRACE naming a log. Every process of that run
(the test process and any Python child it starts) then appends the absolute
path of each file it opened, and the source file of every module it
imported (a cached import opens only its bytecode), when it exits. The
runner holds that list to the CI job's path filter: a file the tests read
that the filter does not cover fails the run. Without TOPOLOGY_READ_TRACE
this does nothing.
"""

from __future__ import annotations

import atexit
import os
import sys

_LOG = os.environ.get("TOPOLOGY_READ_TRACE", "")

if _LOG:
    _seen: set[str] = set()
    _writing = [False]

    def _hook(event: str, args: tuple) -> None:
        if _writing[0] or not args:
            return
        if event == "subprocess.Popen":
            # A file a child process is given by path is read too, though the
            # child may run without this directory on its PYTHONPATH.
            _, argv, cwd, _env = args
            for item in argv if isinstance(argv, (list, tuple)) else []:
                if isinstance(item, (str, bytes, os.PathLike)):
                    candidate = os.path.join(os.fsdecode(cwd) if cwd else os.getcwd(), os.fsdecode(item))
                    if os.path.isfile(candidate):
                        _seen.add(os.path.abspath(candidate))
            return
        if event != "open":
            return
        target = args[0]
        if isinstance(target, bytes):
            target = os.fsdecode(target)
        if isinstance(target, str):
            _seen.add(os.path.abspath(target))

    def _source(path: str) -> str:
        # A module's cached bytecode stands for its source file.
        folder, name = os.path.split(path)
        if os.path.basename(folder) == "__pycache__" and name.endswith(".pyc"):
            return os.path.join(os.path.dirname(folder), name.split(".", 1)[0] + ".py")
        return path

    def _flush() -> None:
        _writing[0] = True
        for module in list(sys.modules.values()):
            file = getattr(module, "__file__", None)
            if isinstance(file, str):
                _seen.add(os.path.abspath(file))
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write("".join(f"{path}\n" for path in sorted({_source(item) for item in _seen})))

    sys.addaudithook(_hook)
    atexit.register(_flush)
