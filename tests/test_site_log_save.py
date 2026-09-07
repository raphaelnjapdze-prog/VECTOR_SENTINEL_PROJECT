"""The site-log page must survive a successful save.

The whole post-save block — success message, QR label, photo preview — was rendered
inside `with st.form(...)`, and render_specimen_qr() calls st.download_button(), which
Streamlit refuses to draw inside a form. So the entry saved to the database and the page
then crashed with StreamlitAPIException while drawing the label for it: the user saw a
traceback instead of the QR code they need to attach to the physical specimen.

Driven through AppTest because no pure-function test can see it — the exception comes
from Streamlit's own form/widget rules at render time.
"""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


@pytest.fixture
def saved_rows(monkeypatch):
    """Capture site-log writes instead of hitting Supabase."""
    calls = []

    import components.site_log as site_log

    def fake_submit(**kwargs):
        calls.append(kwargs)
        return {"specimen_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "photo_urls": []}

    monkeypatch.setattr(site_log, "submit_site_log_entry", fake_submit)
    monkeypatch.setattr(site_log, "load_specimen_records", lambda: pd.DataFrame())
    monkeypatch.setattr(site_log, "clear_specimen_records_cache", lambda: None)
    return calls


def _site_log_app():
    import components.site_log as site_log

    site_log.render_site_log_page()


class TestVialedLabelsSurviveTheirOwnDownload:
    """The QR labels for freshly vialed specimens were drawn inside an `if st.button(...)`
    block, so they lived only for the run that created them. Clicking one label's download
    button triggers a rerun on which that block is False — so every label, including the
    one being downloaded, vanished before the user could print the rest of the tubes."""

    def test_labels_persist_across_a_rerun(self, monkeypatch):
        import components.site_log as site_log

        batch = {
            "specimen_id": "batch-1",
            "collection_date": "2026-07-13",
            "breeding_site_type": "Puddle",
            "field_screening_result": {
                "screening_method": "manual_field_log",
                "result": {"anopheles_count": 10},
            },
        }
        children = [
            {"specimen_id": "child-1", "tube_label": "T-1"},
            {"specimen_id": "child-2", "tube_label": "T-2"},
        ]

        monkeypatch.setattr(site_log, "load_specimen_records", lambda: pd.DataFrame([batch]))
        monkeypatch.setattr(site_log, "available_to_vial", lambda row, genus: 10 if genus == "Anopheles" else 0)
        monkeypatch.setattr(site_log, "vial_out_specimens", lambda *a, **k: children)
        monkeypatch.setattr(site_log, "fetch_batch_children", lambda _bid: pd.DataFrame())
        monkeypatch.setattr(site_log, "submit_site_log_entry", lambda **k: None)
        monkeypatch.setattr(site_log, "clear_specimen_records_cache", lambda: None)

        at = AppTest.from_function(_site_log_app, default_timeout=30)
        at.run()

        at.button(key="subsample_go").click().run()
        assert any("Vialed out 2" in s.value for s in at.success)

        at.run()  # the rerun a label's download button would cause

        assert not at.exception
        assert any("Vialed out 2" in s.value for s in at.success)


class TestSaveRendersTheQrLabel:
    def test_successful_save_does_not_crash_the_page(self, saved_rows):
        at = AppTest.from_function(_site_log_app, default_timeout=30)
        at.run()

        at.button[0].click().run()  # the form's submit button

        # The row reached the ledger, and the page rendered its QR label without
        # tripping Streamlit's "no download_button inside st.form" rule.
        assert len(saved_rows) == 1
        assert not at.exception
        assert any("aaaaaaaa" in s.value for s in at.success)

    def test_label_survives_the_rerun_a_download_click_causes(self, saved_rows):
        at = AppTest.from_function(_site_log_app, default_timeout=30)
        at.run()
        at.button[0].click().run()

        at.run()  # any later rerun, e.g. the one clicking the download button triggers

        # A label drawn once and then dropped would take its own download with it.
        assert not at.exception
        assert any("aaaaaaaa" in s.value for s in at.success)


class TestGpsCaptureModes:
    def test_live_location_cookie_is_consumed_when_present(self, monkeypatch):
        import components.site_log as site_log

        class FakeContext:
            cookies = {
                "vs_geo_lat": "11.806500",
                "vs_geo_lon": "13.153000",
            }

        monkeypatch.setattr(site_log.st, "context", FakeContext(), raising=False)

        assert site_log._read_device_gps_cookie() == (11.8065, 13.153)
