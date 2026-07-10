# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Sphinx configuration for the genropy-asgi documentation."""

import importlib.metadata

project = "genropy-asgi"
author = "Genropy Team"
copyright = "2025, Softwell S.r.l."

try:
    release = importlib.metadata.version("genropy-asgi")
except importlib.metadata.PackageNotFoundError:
    release = "0.1.0"
version = ".".join(release.split(".")[:2])

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "internal"]

# The internal/ working notes are marked DA REVISIONARE and are not user docs.

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_title = f"genropy-asgi {release}"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# autodoc: the package imports gnr.* at runtime; keep the API pages import-light.
autodoc_mock_imports = ["gnr", "genro_asgi", "genro_bag", "httpx"]
autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
}
