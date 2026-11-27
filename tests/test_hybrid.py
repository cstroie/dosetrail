"""Tests for hybrid.py: PET/CT and SPECT/CT hybrid studies."""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from dozimetrix.db.session import make_sessionmaker
from dozimetrix.db.models import Base, Study, Event
from dozimetrix import hybrid


class TestHybrid:
    def setup_method(self):
        engine = create_engine("sqlite:///:memory:")
        maker = make_sessionmaker(engine)
        with maker() as s:
            Base.metadata.create_all(engine)
        self.maker = maker

    def test_nm_modalities(self):
        assert "PT" in hybrid.NM_MODALITIES
        assert "NM" in hybrid.NM_MODALITIES

    def test_label_pet_ct(self):
        # PET study with CT dose (DLP) -> PET/CT
        assert hybrid.label("PT", 500.0) == "PET/CT"
        # SPECT study with CT dose -> SPECT/CT
        assert hybrid.label("NM", 300.0) == "SPECT/CT"

    def test_label_no_ct_dose(self):
        # PET study without CT dose -> PT
        assert hybrid.label("PT", None) == "PT"
        assert hybrid.label("PT", 0.0) == "PT"
        # NM study without CT dose -> NM
        assert hybrid.label("NM", None) == "NM"

    def test_label_other_modalities(self):
        # CT study -> CT
        assert hybrid.label("CT", 500.0) == "CT"
        # DX study -> DX
        assert hybrid.label("DX", None) == "DX"

    def test_has_ct_part(self):
        assert hybrid.has_ct_part("PT", 500.0) is True
        assert hybrid.has_ct_part("NM", 300.0) is True
        assert hybrid.has_ct_part("PT", None) is False
        assert hybrid.has_ct_part("PT", 0.0) is False
        # CT itself is not "has CT part" - that's for hybrid
        assert hybrid.has_ct_part("CT", 500.0) is False
        assert hybrid.has_ct_part("DX", None) is False

    def test_matches_modality_list(self):
        # Hybrid PET/CT matches CT in the list
        assert hybrid.matches("PT", 500.0, ["CT"]) is True
        # Pure PET does not match CT
        assert hybrid.matches("PT", None, ["CT"]) is False
        # Pure NM does not match CT
        assert hybrid.matches("NM", None, ["CT"]) is False
        # Hybrid SPECT/CT matches CT
        assert hybrid.matches("NM", 200.0, ["CT"]) is True
        # CT matches CT
        assert hybrid.matches("CT", 500.0, ["CT"]) is True
        # Multiple modalities
        assert hybrid.matches("PT", 500.0, ["CT", "MR"]) is True

    def test_modality_in_filter(self):
        # SQLAlchemy filter helper
        from sqlalchemy import select
        from dozimetrix.db.models import Study
        
        # This tests that the filter can be used in queries
        stmt = select(Study).where(hybrid.modality_in(["CT", "PT"]))
        # Just verify it compiles
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "study.modality" in compiled


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
