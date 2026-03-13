# Purpose: Top-level package initialization for Closed Claw.

# Suppress langchain_core pydantic.v1 warning on Python 3.14+
import warnings as _warnings
_warnings.filterwarnings(
    "ignore",
    message=r".*Pydantic V1.*isn't compatible with Python 3\.14.*",
    category=UserWarning,
)

