"""Compatibility entry point for the packaged vhotseat proxy."""
from vhotseat import proxy as _proxy
from vhotseat.proxy import *  # noqa: F403


def __getattr__(name):
    return getattr(_proxy, name)


if __name__ == "__main__":
    raise SystemExit(_proxy.main())
