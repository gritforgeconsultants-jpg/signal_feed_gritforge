"""Stage (b) collectors. Each module exposes collect(source, fetcher, today) -> CollectResult.

The allowlist entry's "collector": {"type": ...} block picks the module.
scripts/collect.py is the only caller that writes to the store.
"""
from . import agendas, ckan, sam_gov, socrata

COLLECTORS = {
    "sam_gov": sam_gov.collect,
    "socrata": socrata.collect,
    "ckan": ckan.collect,
    "legistar": agendas.collect,
    "civicplus": agendas.collect,
    "granicus": agendas.collect,
}
