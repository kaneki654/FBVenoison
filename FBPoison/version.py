"""Single source of truth for the build number.

release.py rewrites APP_VERSION when it publishes, and updater.py compares
this value against the manifest served by the update site.
"""

APP_VERSION = "2.0.14"
