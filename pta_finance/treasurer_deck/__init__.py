"""Treasurer update Google Slides generator (Phase 5).

This package turns rough request text plus private finance sources into one strict,
reviewable, provenance-bearing *run* (plan: ``documentation/treasurer-slides-plan.md``).
Step 14 ships the foundations:

* :mod:`pta_finance.treasurer_deck.models` — versioned run/brief/fact/dataset contracts,
  canonical JSON hashing, run-ID/path containment, the run state machine, and locked
  atomic manifest-last persistence.
* :mod:`pta_finance.treasurer_deck.intake` — private request-file validation and the
  deterministic cleaner that separates tasks from workflow guidance and logs every
  ignored fragment with source-span provenance.
* :mod:`pta_finance.treasurer_deck.sources` — the dedicated read-only Sheets reader,
  typed grid provenance, briefing/override fact ingestion, source precedence, and the
  strict schema-v2 reimbursement summary adapter.

Everything a run produces is private and gitignored (``reports/output/treasurer-slides/``).
No module in this package can write to a Google resource.
"""
