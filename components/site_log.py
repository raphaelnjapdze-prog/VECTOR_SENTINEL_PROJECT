"""
Single Site Log Entry — replaces CSV upload entirely.

One form, one save button, one photo field, writes straight to
specimen_records via utils.data_manager.submit_site_log_entry(). No
fabricated success states: a save either genuinely succeeds (confirmed by
the returned row) or shows a real error.
"""

import json
from datetime import date

import pandas as pd
import streamlit as st

from utils.auth import (
    get_current_user_id,
    is_current_user_admin,
)
from utils.logging_config import get_logger
from utils.data_manager import (
    available_to_vial,
    clear_specimen_records_cache,
    delete_specimen_records,
    extract_collector_display,
    fetch_batch_children,
    load_specimen_records,
    owns_row,
    submit_site_log_entry,
    sync_pending_writes,
    vial_out_specimens,
)
from utils.geography import BORNO_LGAS, DEFAULT_LGA
from utils.icons import render_page_header
from utils.offline_queue import clear_quarantine, get_quarantine, pending_count
from utils.pcr_and_accuracy import render_specimen_qr

logger = get_logger(__name__)

_SUBSAMPLE_GENERA = ["Anopheles", "Culex", "Aedes"]
GPS_LAT_COOKIE = "vs_geo_lat"
GPS_LON_COOKIE = "vs_geo_lon"

_BREEDING_SITE_OPTIONS = [
    "Stagnant pool",
    "Swamp / marsh margin",
    "Rice field / irrigated field",
    "Rain pool (temporary)",
    "Concrete water tank",
    "Plastic container / drum",
    "Tyre / discarded container",
    "Drainage channel / gutter",
    "Rock pool",
    "River margin / backwater",
    "Other (specify in notes)",
]


def _validate(anoph, culex, aedes, other, lat, lon, has_gps) -> str | None:
    """Returns an error message if invalid, else None."""
    if any(v < 0 for v in [anoph, culex, aedes, other]):
        return "Counts cannot be negative."
    if has_gps:
        if lat is None or lon is None:
            return "GPS was enabled but latitude/longitude is missing."
        if not (-90.0 <= lat <= 90.0):
            return "Latitude must be between -90 and 90."
        if not (-180.0 <= lon <= 180.0):
            return "Longitude must be between -180 and 180."
    return None


def _plural(n: int) -> str:
    return f"{n} entr{'y' if n == 1 else 'ies'}"


def _read_device_gps_cookie() -> tuple[float, float] | None:
    """Read the last device-captured coordinates saved by the browser JS."""
    try:
        cookies = st.context.cookies or {}
    except Exception:
        logger.debug("st.context.cookies unavailable for device GPS", exc_info=True)
        return None

    lat_raw = cookies.get(GPS_LAT_COOKIE)
    lon_raw = cookies.get(GPS_LON_COOKIE)
    if lat_raw is None or lon_raw is None:
        return None

    try:
        lat = float(lat_raw)
        lon = float(lon_raw)
    except (TypeError, ValueError):
        logger.debug("Device GPS cookies were not valid floats: %r / %r", lat_raw, lon_raw)
        return None

    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        logger.warning("Device GPS cookies were outside valid bounds: %s, %s", lat, lon)
        return None

    return lat, lon


def _render_device_gps_capture_script() -> None:
    """Ask the browser for the current location and save the result in cookies."""
    st.iframe(
        """
        <script>
          (function () {
            function clearCookie(name) {
              var proto = '';
              try { proto = window.parent.location.protocol; } catch (e) {}
              var secure = proto === 'https:' ? '; Secure' : '';
              document.cookie = name + '=; Path=/; Max-Age=0; SameSite=Lax' + secure;
            }
            if (!navigator || !navigator.geolocation) {
              clearCookie('vs_geo_lat');
              clearCookie('vs_geo_lon');
              return;
            }

            navigator.geolocation.getCurrentPosition(
              function (position) {
                var lat = position.coords.latitude;
                var lon = position.coords.longitude;
                var proto = '';
                try { proto = window.parent.location.protocol; } catch (e) {}
                var secure = proto === 'https:' ? '; Secure' : '';
                document.cookie = 'vs_geo_lat=' + lat + '; Path=/; Max-Age=300; SameSite=Lax' + secure;
                document.cookie = 'vs_geo_lon=' + lon + '; Path=/; Max-Age=300; SameSite=Lax' + secure;
                try { window.parent.location.reload(); } catch (e) {}
              },
              function () {
                clearCookie('vs_geo_lat');
                clearCookie('vs_geo_lon');
              },
              { enableHighAccuracy: true, timeout: 15000, maximumAge: 60000 }
            );
          })();
        </script>
        """,
        height=1,
    )


def _render_rejected_entries():
    """Surface entries the server permanently refused.

    These will never sync, so the honest thing is to say so plainly and hand the user
    their data back rather than leave it cycling in a queue that claims it's 'waiting
    to upload'. The download is the last copy — clearing is a deliberate second step.
    """
    rejected = get_quarantine()
    if not rejected:
        return

    st.error(
        f"⚠️ {_plural(len(rejected))} could not be uploaded and will not sync — the "
        "server refused the data. Download them below and re-enter or report them; "
        "they are not saved anywhere else."
    )
    for entry in rejected:
        payload = entry.get("payload", {})
        with st.expander(f"Rejected: {payload.get('specimen_id', 'unknown specimen')}"):
            st.caption(f"Queued at {entry.get('queued_at', 'unknown time')}")
            st.code(entry.get("error") or "No error detail recorded.", language=None)
            st.json(payload)

    st.download_button(
        "Download rejected entries (JSON)",
        data=json.dumps(rejected, indent=2, default=str),
        file_name="rejected_field_entries.json",
        mime="application/json",
        key="offline_rejected_download",
    )
    if st.button("Discard rejected entries", key="offline_rejected_clear"):
        clear_quarantine()
        st.rerun()


def _render_offline_banner():
    """Show and drain the offline queue.

    When entries are waiting, try once to sync them automatically (a cheap no-op if
    still offline — drain stops at the first transient failure), then show what remains
    with a manual retry. Nothing renders when there's nothing pending or rejected, so an
    online user never sees it.
    """
    if pending_count() == 0 and not get_quarantine():
        return

    # Best-effort automatic sync on load — the common case is that connectivity has
    # come back by the time the user returns to this page.
    # The rejected count is not read here — _render_rejected_entries reads the
    # quarantine itself, so it also shows entries rejected on an earlier rerun.
    synced, remaining, _ = sync_pending_writes()
    if synced:
        clear_specimen_records_cache()
        st.success(f"Synced {_plural(synced)} that were saved offline.")

    if remaining:
        st.warning(
            f"📥 {_plural(remaining)} saved on this device but not yet uploaded — "
            "they'll sync automatically when you're back online. They survive a reload; "
            "don't clear your browser data until they've synced."
        )
        if st.button("Sync now", key="offline_sync_now"):
            with st.spinner("Syncing…"):
                s2, r2, _ = sync_pending_writes()
            if s2:
                clear_specimen_records_cache()
                st.success(f"Synced {_plural(s2)}.")
            if r2:
                st.error(f"{r2} still not synced — you're likely still offline. Try again shortly.")
            st.rerun()

    _render_rejected_entries()


def render_site_log_page():
    render_page_header("Site Log Entry", "log")
    st.caption(
        "Log one field observation directly — counts, habitat, GPS, and an "
        "optional photo. Saved immediately to specimen_records; no upload "
        "step, no intermediate file."
    )

    _render_offline_banner()

    # GPS toggle lives OUTSIDE the form so its conditional fields can
    # actually appear before submission — st.form doesn't rerun on
    # interior widget changes until the submit button is pressed.
    has_gps = st.checkbox("I have GPS coordinates for this site", key="site_log_has_gps")
    gps_lat, gps_lon = None, None
    if has_gps:
        gps_mode = st.radio(
            "Coordinate source",
            ["Manual entry", "Use current location"],
            horizontal=True,
            key="site_log_gps_mode",
        )

        if gps_mode == "Use current location":
            device_coords = _read_device_gps_cookie()
            location_btn = st.button(
                "Fetch current location",
                key="site_log_fetch_location",
                use_container_width=True,
            )
            if location_btn:
                _render_device_gps_capture_script()
                st.caption("Requesting device location…")
            if device_coords:
                gps_lat, gps_lon = device_coords
                st.success(f"Location captured: {gps_lat:.6f}, {gps_lon:.6f}")
            else:
                st.info("Use the button above to capture the current device coordinates.")
        else:
            gcol1, gcol2 = st.columns(2)
            with gcol1:
                gps_lat = st.number_input(
                    "Latitude", value=11.8000, format="%.6f", min_value=-90.0, max_value=90.0,
                    key="site_log_lat",
                )
            with gcol2:
                gps_lon = st.number_input(
                    "Longitude", value=13.1500, format="%.6f", min_value=-180.0, max_value=180.0,
                    key="site_log_lon",
                )

    with st.form("site_log_form", clear_on_submit=True):
        col1, col2 = st.columns(2)

        with col1:
            collection_date = st.date_input("Collection date", value=date.today())
            lga = st.selectbox(
                "LGA", BORNO_LGAS, index=BORNO_LGAS.index(DEFAULT_LGA),
                help="Local Government Area of this collection. Becomes the org unit on "
                     "the DHIS2 export — a habitat type cannot serve as one.",
            )
            breeding_site_type = st.selectbox("Breeding site type", _BREEDING_SITE_OPTIONS)

        with col2:
            anopheles_count = st.number_input("Anopheles count", min_value=0, value=0, step=1)
            culex_count = st.number_input("Culex count", min_value=0, value=0, step=1)
            aedes_count = st.number_input("Aedes count", min_value=0, value=0, step=1)
            other_genera_count = st.number_input("Other genera count", min_value=0, value=0, step=1)

        field_notes = st.text_area(
            "Field notes",
            placeholder="Water clarity, vegetation, nearby dwellings, anything relevant...",
        )

        photo_file = st.file_uploader(
            "Field photo (optional)", type=["png", "jpg", "jpeg"],
            help="Uploaded to the specimen-photos storage bucket and linked to this entry.",
        )

        submitted = st.form_submit_button("Save Site Log Entry", type="primary", width="stretch")

        if submitted:
            st.session_state.pop("site_log_saved", None)
            error = _validate(
                anopheles_count, culex_count, aedes_count, other_genera_count,
                gps_lat, gps_lon, has_gps,
            )
            if error:
                st.error(error)
            else:
                with st.spinner("Saving entry..."):
                    saved = submit_site_log_entry(
                        collection_date=collection_date,
                        breeding_site_type=breeding_site_type,
                        lga=lga,
                        gps_lat=gps_lat,
                        gps_lon=gps_lon,
                        anopheles_count=anopheles_count,
                        culex_count=culex_count,
                        aedes_count=aedes_count,
                        other_genera_count=other_genera_count,
                        field_notes=field_notes,
                        photo_file=photo_file,
                    )
                if saved:
                    clear_specimen_records_cache()
                    # Hand the saved row out of the form to be rendered below. The QR label
                    # offers a download, and Streamlit refuses st.download_button inside an
                    # st.form — drawing it here crashed the page on every successful save.
                    st.session_state["site_log_saved"] = saved
                else:
                    # submit_site_log_entry surfaces its own error (not configured, not
                    # signed in, insert rejected); a generic message here would bury it.
                    pass

    # Rendered outside the form, and kept in session_state rather than drawn once: clicking
    # the QR download button triggers a rerun, and a label that vanished on that rerun would
    # take its own download with it. It clears on the next submit.
    saved = st.session_state.get("site_log_saved")
    if saved:
        if saved.get("_pending_offline"):
            st.warning(
                f"Saved on this device — will upload automatically when you're back "
                f"online. Specimen ID: {saved['specimen_id']}"
            )
        else:
            st.success(f"Saved. Specimen ID: {saved['specimen_id']}")
        st.caption("Print this QR label and attach it to the physical specimen so the lab can link it to PCR results.")
        label_col, photo_col = st.columns(2)
        with label_col:
            render_specimen_qr(saved["specimen_id"], key="qr_sitelog_save")
        with photo_col:
            if saved.get("_pending_offline"):
                st.caption("Photo can't be uploaded offline — re-attach it once this entry has synced.")
            elif saved.get("photo_urls"):
                st.image(saved["photo_urls"][0], caption="Uploaded photo", width=200)
            else:
                st.caption("No photo was attached to this entry.")

    st.markdown("---")
    _render_subsampling()

    st.markdown("---")
    _render_recent_entries()


def _batch_label(row) -> str:
    """Human-readable label for a batch in the subsampling selector, showing how many
    of each genus remain available to vial out."""
    avail = " ".join(
        f"{g[:2]}:{available_to_vial(row, g)}" for g in _SUBSAMPLE_GENERA
    )
    date_str = str(row.get("collection_date") or "?")
    site = row.get("breeding_site_type") or "site n/a"
    return f"{date_str} · {site} · [{avail}] · {str(row.get('specimen_id'))[:8]}"


def _render_child_labels(children: list, key_prefix: str) -> None:
    """Render a printable QR label per vialed specimen, three across."""
    for start in range(0, len(children), 3):
        cols = st.columns(3)
        for col, child in zip(cols, children[start:start + 3]):
            with col:
                tube = child.get("tube_label")
                caption = f"{tube}" if tube else str(child["specimen_id"])[:8]
                render_specimen_qr(
                    child["specimen_id"],
                    caption=caption,
                    key=f"{key_prefix}_{child['specimen_id']}",
                    width=150,
                )


def _render_subsampling():
    """Vial out individual specimens from a batch field-count log so each can be
    barcoded, identified, and PCR-confirmed on its own — matching a single
    morphological ID to a single molecular result per mosquito."""
    st.subheader("Subsample Specimens for PCR")
    st.caption(
        "A field count logs many mosquitoes as one batch. To confirm species by PCR, "
        "'vial out' individual specimens: each gets its own QR/barcode and can be "
        "identified and PCR-confirmed separately. The batch's original counts are kept "
        "intact — vialed specimens are just tracked individually so totals never double-count."
    )

    df = load_specimen_records()
    if df.empty or "field_screening_result" not in df.columns:
        st.info("No batch field-count logs yet. Save a site log entry above first.")
        return

    def _is_field_log(result):
        return isinstance(result, dict) and result.get("screening_method") == "manual_field_log"

    batches = df[df["field_screening_result"].apply(_is_field_log)].copy()
    if batches.empty:
        st.info("No batch field-count logs yet. Save a site log entry above first.")
        return

    batch_rows = batches.to_dict("records")
    labels = {_batch_label(r): r for r in batch_rows}
    chosen_label = st.selectbox("Batch collection event", list(labels.keys()), key="subsample_batch")
    batch = labels[chosen_label]
    batch_id = batch["specimen_id"]

    available = {g: available_to_vial(batch, g) for g in _SUBSAMPLE_GENERA}
    selectable = [g for g, n in available.items() if n > 0]

    if not selectable:
        st.warning("Every counted specimen in this batch has already been vialed out.")
    else:
        col1, col2, col3 = st.columns([2, 1, 2])
        with col1:
            genus = st.selectbox(
                "Genus to subsample",
                selectable,
                format_func=lambda g: f"{g} ({available[g]} available)",
                key="subsample_genus",
            )
        with col2:
            count = st.number_input(
                "How many", min_value=1, max_value=int(available[genus]), value=1, step=1,
                key="subsample_count",
            )
        with col3:
            tube_prefix = st.text_input(
                "Tube label prefix (optional)", placeholder="e.g. LAB-2026-07",
                key="subsample_prefix",
            )

        if st.button(f"Vial out {int(count)} {genus} specimen(s)", type="primary", key="subsample_go"):
            with st.spinner("Creating individual specimen records…"):
                children = vial_out_specimens(
                    batch_id, genus, int(count), tube_prefix.strip() or None
                )
            if children:
                st.session_state["vialed_children"] = {
                    "batch_id": batch_id, "genus": genus, "children": children,
                }

    # Rendered outside the button block, from session_state. Drawn inline, these labels
    # lived only for the run that created them: clicking one label's download button
    # triggers a rerun, the button block is False on it, and every label — including the
    # one being downloaded — disappeared before the user could print the rest.
    #
    # Tied to the batch that produced them, so selecting a different batch doesn't leave
    # the previous batch's labels sitting under it, inviting them onto the wrong tubes.
    fresh = st.session_state.get("vialed_children")
    if fresh and fresh.get("children") and fresh.get("batch_id") == batch_id:
        st.success(
            f"Vialed out {len(fresh['children'])} {fresh['genus']} specimen(s). Print each "
            f"label and attach it to that specimen's tube — scan it later to record the "
            f"morphological ID and PCR result for that individual."
        )
        _render_child_labels(fresh["children"], key_prefix="fresh")
        if st.button("Done printing labels", key="subsample_dismiss"):
            st.session_state.pop("vialed_children", None)
            st.rerun()

    # Existing individuals already vialed out of this batch (for reprint / PCR tracking).
    existing = fetch_batch_children(batch_id)
    if not existing.empty:
        with st.expander(f"Individuals already vialed from this batch ({len(existing)})"):
            cols = [c for c in ["tube_label", "specimen_id", "pcr_status", "pcr_confirmed_species"] if c in existing.columns]
            st.dataframe(
                existing[cols].rename(columns={
                    "tube_label": "Tube",
                    "specimen_id": "Specimen ID (QR)",
                    "pcr_status": "PCR Status",
                    "pcr_confirmed_species": "PCR Species",
                }),
                width="stretch",
                hide_index=True,
            )


def _render_recent_entries():
    st.subheader("Recently Logged Entries")

    # Before the early returns below: deleting the last entry empties the table, and the
    # report of that deletion must still be shown rather than vanish with the rows.
    _render_delete_summary()

    df = load_specimen_records()
    if df.empty:
        st.info("No entries logged yet.")
        return

    def _is_field_log(result):
        if isinstance(result, dict):
            return result.get("screening_method") == "manual_field_log"
        return False

    log_df = df[df["field_screening_result"].apply(_is_field_log)].copy()
    if log_df.empty:
        st.info("No site log entries yet — identification-only records exist, but no field counts.")
        return

    log_df["collection_date"] = pd.to_datetime(log_df["collection_date"], errors="coerce")
    log_df = log_df.sort_values("collection_date", ascending=False)

    def _counts(result):
        r = result.get("result", {})
        return f"An:{r.get('anopheles_count',0)} Cx:{r.get('culex_count',0)} Ae:{r.get('aedes_count',0)}"

    log_df["counts"] = log_df["field_screening_result"].apply(_counts)
    log_df["collector"] = [extract_collector_display(row) for row in log_df.to_dict("records")]

    display_cols = ["collection_date", "breeding_site_type", "counts", "collector", "pcr_status"]
    display_cols = [c for c in display_cols if c in log_df.columns]

    st.dataframe(
        log_df[display_cols].head(10).rename(columns={
            "collection_date": "Date",
            "breeding_site_type": "Site Type",
            "counts": "Counts",
            "collector": "Collector",
            "pcr_status": "PCR Status",
        }),
        width="stretch",
        hide_index=True,
    )

    _render_delete_entries(log_df)


def _entry_label(row: dict) -> str:
    """A label that identifies one collection event to a human picking it off a list."""
    raw_date = row.get("collection_date")
    # _render_recent_entries has already coerced this column with pd.to_datetime, but an
    # unparseable date lands as NaT, so fall back to whatever is there.
    when = raw_date.strftime("%Y-%m-%d") if isinstance(raw_date, pd.Timestamp) else str(raw_date or "no date")
    site = row.get("breeding_site_type") or "unspecified site"
    return f"{when} · {site} · {row.get('counts', '')} · {str(row.get('specimen_id', ''))[:8]}"


def _render_delete_entries(log_df: pd.DataFrame) -> None:
    """Delete site-log entries — the cleanup path for trial runs and demo data.

    Kept behind an expander and a typed confirmation because it is irreversible and
    deletes more than it is handed: a batch's vialed-out individuals go with it (they
    cannot outlive the collection event they came from), and each entry's photos are
    removed from storage. Deleting the row alone was what left dead photo URLs and
    orphaned objects behind.

    Pick-and-delete only. The project-wide "delete everything" that used to sit below this
    one now lives on the Profile page, beside the rest of the account's destructive
    actions — there were two controls doing the same irreversible thing with different
    confirmations, and only one of them asked for the delete passkey.
    """
    is_admin = is_current_user_admin()

    with st.expander("🗑️ Delete entries (irreversible)"):
        _render_own_entry_picker(log_df, is_admin)
        if is_admin:
            st.caption(
                "Clearing **every** entry in the project is on the **Profile** page, "
                "under *Reset Logged Data*."
            )


def _render_own_entry_picker(log_df: pd.DataFrame, is_admin: bool) -> None:
    """The pick-and-confirm delete for individual collection events."""
    st.caption(
        "Removes the collection event, every individual specimen vialed out of it, "
        "and its uploaded photos. Use this to clear a trial run before a fresh one — "
        "there is no undo, and real field data deleted here is gone."
    )

    # Only your own entries, unless you are a registered admin. The database refuses
    # the rest either way (sql/add_ownership_delete_policies.sql); leaving them in the
    # picker would just mean offering a checkbox that cannot do anything.
    rows = log_df.to_dict("records")
    if not is_admin:
        user_id = get_current_user_id()
        mine = [row for row in rows if owns_row(row, user_id)]
        hidden = len(rows) - len(mine)
        rows = mine
        if hidden:
            st.caption(
                f"{hidden} entr{'y' if hidden == 1 else 'ies'} recorded by other "
                "investigators are not shown — you can only delete your own."
            )

    labels = {_entry_label(row): row["specimen_id"] for row in rows if row.get("specimen_id")}
    if not labels:
        st.info("No entries of yours are available to delete.")
        return

    chosen = st.multiselect(
        "Entries to delete",
        list(labels.keys()),
        key="sitelog_delete_pick",
        help="Pick one or more collection events.",
    )
    if not chosen:
        return

    # Name the collateral before asking for confirmation. A batch with 40 vialed-out
    # individuals looks like one row here, and the user should know that before typing.
    child_total = 0
    for label in chosen:
        children = fetch_batch_children(labels[label])
        child_total += 0 if children.empty else len(children)
    if child_total:
        st.warning(
            f"These {len(chosen)} entr{'y' if len(chosen) == 1 else 'ies'} have "
            f"**{child_total} vialed-out individual specimen(s)** linked to them. "
            "Those will be deleted too — an individual cannot outlive its batch."
        )

    typed = st.text_input(
        f"Type DELETE to remove {len(chosen)} entr{'y' if len(chosen) == 1 else 'ies'}",
        key="sitelog_delete_confirm",
        placeholder="DELETE",
    )
    if st.button(
        "Delete permanently", type="primary", key="sitelog_delete_go",
        disabled=typed.strip().upper() != "DELETE",
    ):
        with st.spinner("Deleting entries, linked specimens and photos…"):
            summary = delete_specimen_records([labels[label] for label in chosen])
        # delete_specimen_records surfaces its own error on failure; never toast a
        # deletion that did not happen.
        if summary:
            st.session_state["sitelog_delete_summary"] = summary
            st.session_state.pop("sitelog_delete_pick", None)
            st.rerun()


def _render_delete_summary() -> None:
    """Report the last deletion after the rerun that performed it, including the parts
    that did not go cleanly — a partial delete must not read as a clean one."""
    summary = st.session_state.pop("sitelog_delete_summary", None)
    if not summary:
        return

    parts = [f"Deleted **{summary['deleted']}** record(s)"]
    if summary.get("cascaded_children"):
        parts.append(f"including **{summary['cascaded_children']}** vialed-out individual(s)")
    if summary.get("photos_removed"):
        parts.append(f"and **{summary['photos_removed']}** photo(s) from storage")
    st.success(" ".join(parts) + ".")

    if summary.get("batches_restored"):
        st.info(
            "Specimens returned to their batch counts, so the collection totals stay "
            "intact and they can be vialed out again."
        )
    if summary.get("refused_not_yours"):
        count = len(summary["refused_not_yours"])
        st.warning(
            f"{count} selected entr{'y was' if count == 1 else 'ies were'} left alone "
            "because another investigator recorded them. You can only delete entries you "
            "recorded yourself."
        )
    if summary.get("not_deleted"):
        st.warning(
            f"{len(summary['not_deleted'])} record(s) were not deleted — the database "
            "refused them. They may belong to another account under row-level security."
        )
    if summary.get("photos_orphaned"):
        st.warning(
            f"{summary['photos_orphaned']} photo(s) are still in storage — the bucket "
            "refused to remove them. The records are gone, so nothing points at those "
            "files any more, but they still count against storage and stay reachable by "
            "their public URL. Either the objects predate sql/add_photo_ownership.sql and "
            "are not yet owner-prefixed (run `python scripts/migrate_photo_paths.py "
            "--apply`), or the bucket has no DELETE policy at all "
            "(sql/add_photo_ownership.sql)."
        )
    if summary.get("tally_failures"):
        st.error(
            f"{len(summary['tally_failures'])} batch tall(y/ies) could not be corrected "
            "after deleting their individuals, so those collection events now report "
            "fewer specimens than were caught. Check the batch counts, and that an UPDATE "
            "policy exists on specimen_records (sql/add_update_policies.sql)."
        )
