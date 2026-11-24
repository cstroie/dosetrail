from dozimetrix.ingest.identity import device_key, real_serial


def test_placeholder_serials_are_not_serials():
    for v in (None, "", " ", "SERIALNO", "UNKNOWN", "0000", "N/A"):
        assert real_serial(v) == ""
    assert real_serial(" 5CA2212593 ") == "5CA2212593"


def test_two_toshibas_with_the_placeholder_serial_stay_apart():
    a = device_key("TOSHIBA", "Aquilion PRIME", "SERIALNO", "ID_STATION", "SPIT. GR. ALEXANDRESCU/AQUILION")
    b = device_key("TOSHIBA", "Astelion", "SERIALNO", "ID_STATION", "Medima Focsani")
    assert a != b


def test_real_serial_still_identifies_across_sites():
    assert device_key("GE", "X", "123", "A", "H1") == device_key("GE", "Y", "123", "B", "H2")
