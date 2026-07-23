"""Standalone-only comtypes typelib version validation.

Generated wrappers must still match the installed comtypes code generator.
Typelib mtimes are machine-specific Windows metadata and cannot be compared
against values captured on the build machine.
"""


def _check_version(actual, tlib_cached_mtime=None):
    from comtypes.tools.codegenerator import version as required

    if actual != required:
        raise ImportError("Wrong version")
