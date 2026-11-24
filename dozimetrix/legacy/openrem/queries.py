"""Read-only SQL run against an OpenREM PostgreSQL database (0.10 and 1.0 schemas).

The joins and per-event aggregation happen server-side; each query yields one flat CSV.
Units are OpenREM's (RDSR) units and are converted by the importer:
DAP Gy·m², RP dose Gy, exposure µAs, exposure time ms, irradiation duration s,
lengths mm, patient size m, weight kg, DLP mGy·cm, CTDIvol mGy, AGD mGy.
"""

from __future__ import annotations

# Code meaning lookup helper
_CM = "(SELECT c.code_meaning FROM remapp_contextid c WHERE c.id = {col})"

STUDIES = """
SELECT g.id AS orem_id, g.study_instance_uid, g.study_date, g.study_time,
       g.accession_number, g.accession_hashed, g.study_description,
       g.procedure_code_meaning, g.requested_procedure_code_meaning,
       g.performing_physician_name, g.operator_name, g.referring_physician_name,
       g.modality_type, g.number_of_events, g.total_dlp, g.total_dap,
       g.total_rp_dose_a, g.total_rp_dose_b, g.total_agd_left, g.total_agd_right,
       pm.patient_name, pm.name_hashed, pm.patient_id, pm.id_hashed,
       pm.patient_birth_date, pm.patient_sex, pm.not_patient_indicator,
       ps.patient_age, ps.patient_age_decimal, ps.patient_size, ps.patient_weight,
       eq.manufacturer, eq.manufacturer_model_name, eq.device_serial_number, eq.station_name,
       eq.institution_name, eq.institutional_department_name, eq.software_versions,
       eq.gantry_id, eq.unique_equipment_name_id,
       u.display_name, u.user_defined_modality, u.device_observer_uid,
       acc.total_fluoro_time, acc.fluoro_dap, acc.acq_dap
FROM remapp_generalstudymoduleattr g
LEFT JOIN LATERAL (SELECT * FROM remapp_patientmoduleattr x
                   WHERE x.general_study_module_attributes_id = g.id ORDER BY x.id LIMIT 1) pm
       ON TRUE
LEFT JOIN LATERAL (SELECT * FROM remapp_patientstudymoduleattr x
                   WHERE x.general_study_module_attributes_id = g.id ORDER BY x.id LIMIT 1) ps
       ON TRUE
LEFT JOIN LATERAL (SELECT * FROM remapp_generalequipmentmoduleattr x
                   WHERE x.general_study_module_attributes_id = g.id ORDER BY x.id LIMIT 1) eq
       ON TRUE
LEFT JOIN remapp_uniqueequipmentnames u ON u.id = eq.unique_equipment_name_id
LEFT JOIN LATERAL (
    SELECT sum(ap.total_fluoro_time) AS total_fluoro_time,
           sum(ap.fluoro_dose_area_product_total) AS fluoro_dap,
           sum(ap.acquisition_dose_area_product_total) AS acq_dap
    FROM remapp_projectionxrayradiationdose p
    JOIN remapp_accumxraydose ax ON ax.projection_xray_radiation_dose_id = p.id
    JOIN remapp_accumprojxraydose ap ON ap.accumulated_xray_dose_id = ax.id
    WHERE p.general_study_module_attributes_id = g.id) acc ON TRUE
ORDER BY g.id
"""

CT_EVENTS = f"""
SELECT r.general_study_module_attributes_id AS orem_study_id, e.id AS orem_event_id,
       e.irradiation_event_uid, to_char(e.date_time_started, 'YYYY-MM-DD HH24:MI:SS') AS started,
       e.acquisition_protocol, e.series_description, e.comment,
       {_CM.format(col="e.ct_acquisition_type_id")} AS acquisition_type,
       {_CM.format(col="e.ctdiw_phantom_type_id")} AS phantom,
       {_CM.format(col="e.target_region_id")} AS target_region,
       e.exposure_time, e.nominal_single_collimation_width, e.nominal_total_collimation_width,
       e.pitch_factor, e.mean_ctdivol, e.dlp, e.xray_modulation_type,
       src.kvp, src.ma, src.ma_max, src.rotation_time,
       sl.scanning_length,
       EXISTS (SELECT 1 FROM remapp_ctdosecheckdetails d
               WHERE d.ct_irradiation_event_data_id = e.id
                 AND (d.dlp_alert_value_configured OR d.ctdivol_alert_value_configured)
              ) AS dose_check_alert
FROM remapp_ctirradiationeventdata e
JOIN remapp_ctradiationdose r ON r.id = e.ct_radiation_dose_id
LEFT JOIN LATERAL (SELECT avg(s.kvp) AS kvp, avg(s.xray_tube_current) AS ma,
                          max(s.maximum_xray_tube_current) AS ma_max,
                          avg(s.exposure_time_per_rotation) AS rotation_time
                   FROM remapp_ctxraysourceparameters s
                   WHERE s.ct_irradiation_event_data_id = e.id) src ON TRUE
LEFT JOIN LATERAL (SELECT x.scanning_length FROM remapp_scanninglength x
                   WHERE x.ct_irradiation_event_data_id = e.id ORDER BY x.id LIMIT 1) sl ON TRUE
ORDER BY r.general_study_module_attributes_id, e.id
"""

PROJ_EVENTS = f"""
SELECT p.general_study_module_attributes_id AS orem_study_id, e.id AS orem_event_id,
       e.irradiation_event_uid, to_char(e.date_time_started, 'YYYY-MM-DD HH24:MI:SS') AS started,
       e.acquisition_protocol, e.comment, e.dose_area_product, e.entrance_exposure_at_rp,
       {_CM.format(col="e.irradiation_event_type_id")} AS event_type,
       {_CM.format(col="e.acquisition_plane_id")} AS plane,
       {_CM.format(col="e.anatomical_structure_id")} AS anatomy,
       {_CM.format(col="e.laterality_id")} AS laterality,
       {_CM.format(col="e.image_view_id")} AS view,
       s.dose_rp, s.average_glandular_dose, s.pulse_rate, s.number_of_pulses,
       s.irradiation_duration, s.average_xray_tube_current, s.exposure_time,
       s.collimated_field_height, s.collimated_field_width, s.exposure_control_mode,
       {_CM.format(col="s.anode_target_material_id")} AS anode_target,
       (SELECT avg(k.kvp) FROM remapp_kvp k
         WHERE k.irradiation_event_xray_source_data_id = s.id) AS kvp,
       (SELECT avg(t.xray_tube_current) FROM remapp_xraytubecurrent t
         WHERE t.irradiation_event_xray_source_data_id = s.id) AS tube_current,
       (SELECT sum(x.exposure) FROM remapp_exposure x
         WHERE x.irradiation_event_xray_source_data_id = s.id) AS exposure_uas,
       (SELECT avg(w.pulse_width) FROM remapp_pulsewidth w
         WHERE w.irradiation_event_xray_source_data_id = s.id) AS pulse_width,
       (SELECT string_agg(coalesce(cm.code_meaning, '?') || ':' ||
                          coalesce(f.xray_filter_thickness_minimum::text, ''), ';' ORDER BY f.id)
          FROM remapp_xrayfilters f
          LEFT JOIN remapp_contextid cm ON cm.id = f.xray_filter_material_id
         WHERE f.irradiation_event_xray_source_data_id = s.id) AS filters,
       (SELECT string_agg(coalesce(cg.code_meaning, '?'), ';' ORDER BY gr.id)
          FROM remapp_xraygrid gr LEFT JOIN remapp_contextid cg ON cg.id = gr.xray_grid_id
         WHERE gr.irradiation_event_xray_source_data_id = s.id) AS grid,
       det.exposure_index, det.target_exposure_index, det.deviation_index,
       det.relative_xray_exposure, det.relative_exposure_unit,
       m.positioner_primary_angle, m.positioner_secondary_angle,
       m.compression_thickness, m.compression_force,
       dist.distance_source_to_detector, dist.distance_source_to_entrance_surface
FROM remapp_irradeventxraydata e
JOIN remapp_projectionxrayradiationdose p ON p.id = e.projection_xray_radiation_dose_id
LEFT JOIN LATERAL (SELECT * FROM remapp_irradeventxraysourcedata x
                   WHERE x.irradiation_event_xray_data_id = e.id ORDER BY x.id LIMIT 1) s ON TRUE
LEFT JOIN LATERAL (SELECT * FROM remapp_irradeventxraydetectordata x
                   WHERE x.irradiation_event_xray_data_id = e.id ORDER BY x.id LIMIT 1) det ON TRUE
LEFT JOIN LATERAL (SELECT * FROM remapp_irradeventxraymechanicaldata x
                   WHERE x.irradiation_event_xray_data_id = e.id ORDER BY x.id LIMIT 1) m ON TRUE
LEFT JOIN LATERAL (SELECT * FROM remapp_doserelateddistancemeasurements x
                   WHERE x.irradiation_event_xray_mechanical_data_id = m.id
                   ORDER BY x.id LIMIT 1) dist ON TRUE
ORDER BY p.general_study_module_attributes_id, e.id
"""

OBJECT_UIDS = """
SELECT o.sop_instance_uid, o.general_study_module_attributes_id AS orem_study_id
FROM remapp_objectuidsprocessed o ORDER BY o.id
"""

# Independent summary computed by OpenREM's own tables – used for reconciliation.
SUMMARY = """
WITH st AS (
    SELECT g.id, coalesce(g.modality_type, '') AS modality,
           coalesce(extract(year FROM g.study_date)::int, 0) AS year,
           g.study_instance_uid, g.total_dlp, g.total_dap
    FROM remapp_generalstudymoduleattr g),
ct AS (
    SELECT r.general_study_module_attributes_id AS sid, count(*) AS n, sum(e.dlp) AS dlp
    FROM remapp_ctirradiationeventdata e
    JOIN remapp_ctradiationdose r ON r.id = e.ct_radiation_dose_id GROUP BY 1),
pj AS (
    SELECT p.general_study_module_attributes_id AS sid, count(*) AS n,
           sum(e.dose_area_product) AS dap
    FROM remapp_irradeventxraydata e
    JOIN remapp_projectionxrayradiationdose p ON p.id = e.projection_xray_radiation_dose_id
    GROUP BY 1)
SELECT st.modality, st.year, count(*) AS studies, count(DISTINCT st.study_instance_uid) AS uids,
       sum(st.total_dlp) AS sum_total_dlp, sum(st.total_dap) AS sum_total_dap_gym2,
       sum(coalesce(ct.n, 0)) AS ct_events, sum(ct.dlp) AS sum_event_dlp,
       sum(coalesce(pj.n, 0)) AS proj_events, sum(pj.dap) AS sum_event_dap_gym2
FROM st LEFT JOIN ct ON ct.sid = st.id LEFT JOIN pj ON pj.sid = st.id
GROUP BY 1, 2 ORDER BY 1, 2
"""

# Per-year totals over all modalities (distinct Study UIDs) – robust to modality relabelling.
SUMMARY_YEAR = """
WITH st AS (
    SELECT g.id, coalesce(extract(year FROM g.study_date)::int, 0) AS year,
           g.study_instance_uid
    FROM remapp_generalstudymoduleattr g),
ct AS (
    SELECT r.general_study_module_attributes_id AS sid, count(*) AS n, sum(e.dlp) AS dlp
    FROM remapp_ctirradiationeventdata e
    JOIN remapp_ctradiationdose r ON r.id = e.ct_radiation_dose_id GROUP BY 1),
pj AS (
    SELECT p.general_study_module_attributes_id AS sid, count(*) AS n,
           sum(e.dose_area_product) AS dap
    FROM remapp_irradeventxraydata e
    JOIN remapp_projectionxrayradiationdose p ON p.id = e.projection_xray_radiation_dose_id
    GROUP BY 1)
SELECT st.year, count(DISTINCT st.study_instance_uid) AS uids,
       sum(coalesce(ct.n, 0)) + sum(coalesce(pj.n, 0)) AS events,
       sum(ct.dlp) AS sum_event_dlp, sum(pj.dap) AS sum_event_dap_gym2
FROM st LEFT JOIN ct ON ct.sid = st.id LEFT JOIN pj ON pj.sid = st.id
GROUP BY 1 ORDER BY 1
"""

SCHEMA_VERSION = """
SELECT name FROM django_migrations WHERE app = 'remapp' ORDER BY id DESC LIMIT 1
"""

DUMPS: dict[str, str] = {
    "studies": STUDIES,
    "ct_events": CT_EVENTS,
    "proj_events": PROJ_EVENTS,
    "object_uids": OBJECT_UIDS,
    "summary": SUMMARY,
    "summary_year": SUMMARY_YEAR,
    "schema_version": SCHEMA_VERSION,
}
