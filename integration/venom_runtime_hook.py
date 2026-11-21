"""PyInstaller hook owned by FBVenoison, outside the upstream app."""
from venom_bridge import apply

apply(defer_auth=True)
