# © 2026 Westfield Innovations LLC. Patent Pending.
# U.S. App. No. 19/739,419 | 63/953,563 | 63/953,842
# MODIFICATION PROHIBITED. USE AS SHIPPED.

"""The Noun Probe as a save filter: a fact that uses a word against its class is refused."""

from pdm_memory.filters.base import FilterVerdict
from pdm_memory.noun_probe.pipeline import FILTER, PROBE_VERSION, audit, misframed


class NounProbeFilter:
    """AUDIT each fact before it is stored; refuse it when a noun is misframed."""

    name = FILTER

    def __init__(
        self, system: str = "pdm-memory", domain: str = "memory", scale: str = "claim"
    ):
        self.system = system
        self.domain = domain
        self.scale = scale

    def check(self, text: str) -> FilterVerdict:
        # force: a fact is far shorter than the gate's committed-document length.
        report = audit(
            text, system=self.system, domain=self.domain, scale=self.scale, force=True
        )
        terms = tuple(f.term for f in misframed(report))
        return FilterVerdict(
            self.name,
            str(PROBE_VERSION),
            ran=report is not None,
            passed=not terms,
            terms=terms,
        )
