"""DICOM nodes: validation, config import, allowed senders, PACS choice, C-ECHO, status."""

import socket

import pytest
from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.sop_class import Verification

from dozimetrix.config import Settings
from dozimetrix.db.models import AppSetting
from dozimetrix.net import nodes
from dozimetrix.net.scp import Receiver
from dozimetrix.net.spool import Spool


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_create_validates(session):
    with pytest.raises(nodes.NodeError):
        nodes.create(session, {"name": "X", "aet": "THIS-AE-TITLE-IS-TOO-LONG"})
    with pytest.raises(nodes.NodeError):
        nodes.create(session, {"name": "X", "aet": "PACS", "query_retrieve": True})  # no host
    n = nodes.create(session, {"name": " CT 1 ", "aet": "CT1", "sends": True,
                               "modalities": "ct, sr"})
    assert n.name == "CT 1" and n.modalities == "CT,SR"
    with pytest.raises(nodes.NodeError):
        nodes.create(session, {"name": "CT 1", "aet": "CT2"})
    n2, changes = nodes.update(session, n.id, {"enabled": False})
    assert changes == {"enabled": {"from": True, "to": False}}


def test_config_import_once_and_node_choice(session):
    s = Settings(pacs_host="pacs.example", pacs_port=104, pacs_aet="PACSAE",
                 pacs_max_results=500)
    n = nodes.import_config(session, s)
    assert n.query_retrieve and n.max_results == 500 and n.host == "pacs.example"
    node, mods = nodes.qr_node(session, s)
    assert (node.host, node.aet, node.max_results) == ("pacs.example", "PACSAE", 500)
    assert "CT" in mods
    nodes.delete(session, n.id)
    assert nodes.import_config(session, s) is None  # a deleted import is not recreated
    node, _ = nodes.qr_node(session, s)  # no node left: the config is the fallback
    assert node.aet == "PACSAE"
    assert nodes.qr_node(session, s, "nope") == (None, None)


def test_allowed_senders(session):
    assert nodes.allowed_senders(session, []) == []  # any caller
    nodes.create(session, {"name": "CT 1", "aet": "CT1", "sends": True})
    nodes.create(session, {"name": "Off", "aet": "OFF", "sends": True, "enabled": False})
    nodes.create(session, {"name": "PACS", "aet": "PACS", "host": "h", "port": 104,
                           "query_retrieve": True})
    assert nodes.allowed_senders(session, ["DX1"]) == ["CT1", "DX1"]


def test_echo_and_receiver_allow_list(session, tmp_path):
    port = _port()
    rx = Receiver(Spool(tmp_path / "spool"), "DOZIMETRIX", port, "127.0.0.1")
    rx.start()
    try:
        n = nodes.create(session, {"name": "Self", "aet": "DOZIMETRIX", "host": "127.0.0.1",
                                   "port": port})
        res = nodes.echo(session, n.id, "TESTER", timeout=5)
        assert res["ok"] is True and res["error"] is None and res["ms"] >= 0
        rx.set_allowed(["ONLYME"])  # changed while running
        res = nodes.echo(session, n.id, "TESTER", timeout=5)
        assert res["ok"] is False and "rejected" in res["error"]
        dead = nodes.create(session, {"name": "Dead", "aet": "X", "host": "127.0.0.1",
                                      "port": _port()})
        assert nodes.echo(session, dead.id, "TESTER", timeout=2)["ok"] is False
    finally:
        rx.stop()


def test_receiver_counts_per_calling_aet(tmp_path):
    from pydicom.uid import ExplicitVRLittleEndian
    from pynetdicom.sop_class import SecondaryCaptureImageStorage

    port = _port()
    rx = Receiver(Spool(tmp_path / "spool"), "DOZIMETRIX", port, "127.0.0.1")
    rx.start()
    try:
        ae = AE(ae_title="MODALITY1")
        ae.add_requested_context(SecondaryCaptureImageStorage, ExplicitVRLittleEndian)
        ae.add_requested_context(Verification)
        assoc = ae.associate("127.0.0.1", port, ae_title="DOZIMETRIX")
        ds = Dataset()
        ds.SOPClassUID, ds.SOPInstanceUID = SecondaryCaptureImageStorage, "1.2.3.4"
        ds.PatientID = "x"
        from pydicom.dataset import FileMetaDataset

        ds.file_meta = FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        assert assoc.send_c_store(ds).Status == 0
        assoc.release()
    finally:
        rx.stop()
    assert rx.by_aet["MODALITY1"]["n"] == 1


def test_status_heartbeat(session):
    nodes.write_status(session, nodes.RECEIVER_KEY, {"received": 3})
    st = nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=60)
    assert st["received"] == 3 and st["running"] is True and "pid" in st
    session.get(AppSetting, nodes.RECEIVER_KEY).value = {**st, "at": "2000-01-01T00:00:00"}
    assert nodes.read_status(session, nodes.RECEIVER_KEY, stale_after=60)["running"] is False
    assert nodes.read_status(session, "nothing", 60) is None
