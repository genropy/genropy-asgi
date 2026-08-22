# Deferred human checks

## Phase 3 — delegated to the comparison bench

Startdoc: `temp/startdoc_test_parallelo_2026-08-22.md` (v1.4). These four are
the collaudo sequence of that bench, recorded as traces rather than checked by
eye:

- [ ] the site opens and logs in
- [ ] navigation updates data (datachanges via collect_page)
- [ ] a commit on a subscribed table reaches the page (dbevents)
- [ ] idle → freeze → a new request wakes the user and the page still receives
      user-store updates
