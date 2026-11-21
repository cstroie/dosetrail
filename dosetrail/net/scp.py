"""DICOM receiver: C-ECHO and C-STORE SCP writing every object to the spool.

The network thread does no database work: it writes the received bytes (preamble + file
meta + dataset, exactly as encoded) to spool/incoming and answers Success. The spool
worker imports them. All storage SOP classes and transfer syntaxes are accepted, because
modalities often send compressed images and only the headers are needed.
"""

from __future__ import annotations

import logging

from pynetdicom import AE, ALL_TRANSFER_SYNTAXES, AllStoragePresentationContexts, evt
from pynetdicom.sop_class import Verification

from .spool import Spool

log = logging.getLogger("dosetrail.scp")


def build_ae(aet: str, allowed_aets: list[str] | None = None) -> AE:
    ae = AE(ae_title=aet)
    for cx in AllStoragePresentationContexts:
        ae.add_supported_context(cx.abstract_syntax, ALL_TRANSFER_SYNTAXES)
    ae.add_supported_context(Verification)
    if allowed_aets:
        ae.require_calling_aet = list(allowed_aets)
    # 0 ("unlimited") is legal DICOM but makes some senders (dcmtk) abort on large
    # objects; 128 KiB is the common maximum and keeps throughput high.
    ae.maximum_pdu_size = 131072
    return ae


class Receiver:
    def __init__(self, spool: Spool, aet: str, port: int, bind: str = "0.0.0.0",
                 allowed_aets: list[str] | None = None):
        self.spool, self.aet, self.port, self.bind = spool, aet, port, bind
        self.ae = build_ae(aet, allowed_aets)
        self.server = None
        self.received = 0

    def _on_store(self, event) -> int:
        uid = str(event.request.AffectedSOPInstanceUID)
        try:
            data = event.encoded_dataset()
            self.spool.write(uid, lambda p: p.write_bytes(data))
        except Exception:
            log.exception("cannot spool %s", uid)
            return 0xA700  # Out of resources
        self.received += 1
        return 0x0000

    def _on_echo(self, event) -> int:
        return 0x0000

    def start(self) -> None:
        handlers = [(evt.EVT_C_STORE, self._on_store), (evt.EVT_C_ECHO, self._on_echo)]
        self.server = self.ae.start_server((self.bind, self.port), block=False,
                                           evt_handlers=handlers)
        log.info("DICOM receiver %s listening on %s:%s", self.aet, self.bind, self.port)

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server = None
