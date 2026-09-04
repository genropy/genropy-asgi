# Copyright 2025 Softwell S.r.l.
# Licensed under the Apache License, Version 2.0

"""Contract: a page's ``user_view`` follows the row's store through the deposit round trip.

A login carries the guest's store; a freeze and an adoption swap the row's Bag;
every page watching the owner's store must be re-attached on the Bag the row
holds NOW, and no change captured before the swap may be lost. Migrated from the
core (genro-asgi ``tests/orchestration/test_contract_phase12_carried_store_views.py``
at ``f3c7962``) with #59.
"""

from __future__ import annotations

from genro_asgi.spa import GUEST_PREFIX


def watching_guest(lane) -> str:
    """A guest with one page holding a user-store window on ``gnr.batch``."""
    lane.worker.add_connection("a1b2")
    lane.worker.add_page("page-0", "a1b2")
    guest = f"{GUEST_PREFIX}a1b2"
    lane.worker.setStoreSubscription(guest, "page-0", "user", "gnr.batch")
    return guest


def test_a_page_still_captures_its_user_store_after_the_deposit_round_trip(lane):
    watching_guest(lane)
    lane.worker.change_connection_user("a1b2", "mario")
    lane.run(lane.worker.freeze_connection("a1b2", f"{GUEST_PREFIX}a1b2"))
    lane.run(lane.worker.adopt_connection("mario", "a1b2"))
    lane.worker.user_register.get("mario")["store"]["gnr.batch.b1"] = "running"
    lane.open_request()
    delivery = lane.verb("collect_page", "page-0")
    assert "gnr.batch.b1" in [change["key"]["path"] for change in delivery["datachanges"]]


def test_the_views_watch_the_rows_current_store_bag_after_adoption(lane):
    watching_guest(lane)
    lane.worker.add_page("page-1", "a1b2")
    lane.worker.setStoreSubscription(f"{GUEST_PREFIX}a1b2", "page-1", "user", "gnr.other")
    lane.worker.change_connection_user("a1b2", "mario")
    lane.run(lane.worker.freeze_connection("a1b2", f"{GUEST_PREFIX}a1b2"))
    lane.run(lane.worker.adopt_connection("mario", "a1b2"))
    row_store = lane.worker.user_register.get("mario")["store"]
    for page_id in ("page-0", "page-1"):
        view = lane.worker.page_register.get(page_id)["user_view"]
        assert view is not None and view.bag is row_store


def test_no_captured_change_is_lost_in_the_swap(lane):
    from gnr.core.gnrbag import Bag

    watching_guest(lane)
    lane.worker.change_connection_user("a1b2", "mario")
    # Captured by the view while it still watches the row's pre-swap Bag.
    lane.worker.user_register.get("mario")["store"]["gnr.batch.b0"] = "queued"
    carried = Bag()
    carried["cart.item"] = "a lamp"
    with lane.worker.dispatch_lock:
        lane.worker._install_carried_store("mario", carried, False)
    # Captured by the re-attached view on the carried Bag.
    lane.worker.user_register.get("mario")["store"]["gnr.batch.b1"] = "running"
    lane.open_request()
    delivery = lane.verb("collect_page", "page-0")
    paths = [change["key"]["path"] for change in delivery["datachanges"]]
    assert paths.index("gnr.batch.b0") < paths.index("gnr.batch.b1")
