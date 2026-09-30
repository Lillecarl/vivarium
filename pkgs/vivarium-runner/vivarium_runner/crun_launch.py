"""``python -m vivarium_runner.crun_launch``: see :func:`vivarium_runner.container.main`.

Its own module, because the package imports :mod:`vivarium_runner.container`
first, and runpy warns when the module it runs is already loaded.
"""

from .container import main

raise SystemExit(main())
