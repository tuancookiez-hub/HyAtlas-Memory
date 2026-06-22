"""Version. Kept in a separate module so the version string is importable
without pulling in heavy deps (kuzu, openai, etc.) that the package
declares. Lets `pip show hyatlas-memory` work cleanly.
"""

__version__ = "1.4.0"
