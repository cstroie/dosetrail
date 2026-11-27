"""Tests for siteconf.py: site-wide settings in DB."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from dozimetrix import siteconf
from dozimetrix.db.session import make_sessionmaker


class TestSiteConf:
    def setup_method(self):
        engine = create_engine("sqlite:///:memory:")
        maker = make_sessionmaker(engine)
        with maker() as s:
            from dozimetrix.db.models import Base
            Base.metadata.create_all(engine)
        self.maker = maker

    def test_stored_returns_empty_dict_initially(self):
        with self.maker() as s:
            assert siteconf.stored(s) == {}

    def test_save_and_stored_roundtrip(self):
        with self.maker() as s:
            siteconf.save(s, {"base_url": "https://dose.example.org", "spool_retention_days": 30})
            s.commit()
        with self.maker() as s:
            stored = siteconf.stored(s)
            assert stored["base_url"] == "https://dose.example.org"
            assert stored["spool_retention_days"] == 30

    def test_save_overwrites_existing(self):
        with self.maker() as s:
            siteconf.save(s, {"base_url": "https://old.example.org"})
            s.commit()
            siteconf.save(s, {"base_url": "https://new.example.org"})
            s.commit()
        with self.maker() as s:
            assert siteconf.stored(s)["base_url"] == "https://new.example.org"

    def test_clean_removes_unknown_keys(self):
        cleaned = siteconf.clean({"base_url": "https://x.org", "unknown": "value"})
        assert "unknown" not in cleaned
        assert cleaned["base_url"] == "https://x.org"

    def test_clean_accepts_known_keys(self):
        # base_url as string
        cleaned = siteconf.clean({"base_url": "https://x.org"})
        assert "base_url" in cleaned
        # spool_retention_days as int
        cleaned = siteconf.clean({"spool_retention_days": 45})
        assert "spool_retention_days" in cleaned

    def test_clean_rejects_invalid_base_url(self):
        with pytest.raises(siteconf.SiteConfError):
            siteconf.clean({"base_url": "not-a-url"})
        with pytest.raises(siteconf.SiteConfError):
            siteconf.clean({"base_url": "ftp://example.org"})

    def test_clean_rejects_invalid_retention(self):
        with pytest.raises(siteconf.SiteConfError):
            siteconf.clean({"spool_retention_days": -1})
        with pytest.raises(siteconf.SiteConfError):
            siteconf.clean({"spool_retention_days": 4000})  # > MAX_RETENTION_DAYS (3650)

    def test_spool_retention_returns_int(self):
        with self.maker() as s:
            siteconf.save(s, {"spool_retention_days": 45})
            s.commit()
            days = siteconf.spool_retention(self.maker, 7)
            assert days == 45

    def test_spool_retention_falls_back_to_config(self):
        with self.maker() as s:
            days = siteconf.spool_retention(self.maker, 7)
            assert days == 7

    def test_spool_retention_zero_means_keep_all(self):
        with self.maker() as s:
            siteconf.save(s, {"spool_retention_days": 0})
            s.commit()
            days = siteconf.spool_retention(self.maker, 7)
            assert days == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
