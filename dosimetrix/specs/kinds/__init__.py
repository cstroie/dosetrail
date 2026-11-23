"""The built-in kinds. Importing this package registers them."""

from .. import (
    extractors,  # noqa: F401  (registers the built-in row extractors)
    fields,  # noqa: F401  (registers the built-in fields and transforms)
)
from . import compare, outliers, records, series, table  # noqa: F401
