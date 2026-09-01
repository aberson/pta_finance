"""Private-input contracts for the optional Treasurer Slides workflow.

The package is deliberately generic. Real statements, finance facts, OAuth material,
and presentation identifiers belong only in gitignored operator inputs and outputs.
The stable contract surface is ``pta_finance.treasurer_slides.models``.
"""

from pta_finance.treasurer_slides.models import TreasurerSlidesError

__all__ = ["TreasurerSlidesError"]
