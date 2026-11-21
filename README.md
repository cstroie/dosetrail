# DoseTrail

**Open-source patient radiation exposure monitoring for medical imaging.**

DoseTrail collects radiation dose data from imaging equipment. It covers CT, radiography (DX/CR), fluoroscopy (RF/XA) and mammography, with nuclear medicine planned. The data is normalised into one database, where you can do the following:

- browse studies and individual irradiation events;
- build dose statistics per protocol, procedure and device (median, boxplot, histogram, trends over time) to support **diagnostic reference levels** and optimisation;
- export data for audits and national surveys;
- raise alerts on high and cumulative fluoroscopy doses;
- ingest data automatically from modalities and PACS: DICOM store, query/retrieve, RDSR and image headers, with vendor-specific adapters.

DoseTrail is a modern alternative to [OpenREM](https://openrem.org/) and can import an existing OpenREM database.

> **Status:** early design phase. See [`docs/design.md`](docs/design.md) and [`docs/openrem-analysis.md`](docs/openrem-analysis.md).

## Stack

- Python 3.12, FastAPI, SQLAlchemy 2 and Alembic.
- pydicom and pynetdicom.
- MariaDB (production) or SQLite (development).
- A lightweight web UI with ECharts.
- Few dependencies by design.

## Licence

MIT. See [LICENSE](LICENSE).
