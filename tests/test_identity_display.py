from dosimetrix.ingest.store import display_name


def test_placeholder_station_falls_back_to_model():
    assert display_name("ID_STATION", "Aquilion Prime SP") == "Aquilion Prime SP"
    assert display_name("CTREVO", "Revolution CT") == "CTREVO"
    assert display_name(None, "X") == "X"
    assert display_name("ID_STATION", None) == "ID_STATION"
