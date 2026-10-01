"""VectorNav VN-200 rover driver (spec-based: vnproglib 1.2 and the VN-200 user manual; no
unit has been on the bench). Importing the package registers the VN frame parser."""

from mtrtk.rover.drivers.vectornav import framer as _framer  # noqa: F401  (registers the parser)
