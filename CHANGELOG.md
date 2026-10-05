## v0.3.0 (2026-10-06)

### Feat

- **hudsonlink**: add Hudson Link source

## v0.2.2 (2026-10-02)

### Fix

- **deps**: bump anyio past CVE-2026-63374

## v0.2.1 (2026-10-01)

## v0.2.0 (2026-10-01)

### BREAKING CHANGE

- the module is now gtfs_zone_rt_pollers

### Feat

- **publish**: batch the ingest calls and require a vehicle_id
- **amtrak**: fetch full alert detail pages for descriptions
- **amtrak**: scrape rider alerts and sync them to cafe-car
- **sources**: publish where the vehicle is along its trip
- **ingest**: publish a public per-vehicle id, distinct from the credential
- map the Albany commuter D PM run
- replace --wait with a --watch loop that captures routes as they wake
- map the Albany commuter PM run stops
- map the Albany commuter PM run
- add --wait to the buswhere map builder
- add BuswhereSource (Columbia County)
- add buswhere→GTFS ID map generator and fixture
- add route-window trip resolution to GtfsResolver
- resolve trip instances by start_date, emit route_id
- emit Amtrak per-stop trip-updates via ingest; drop MQTT
- publish Amtrak positions to cafe-car ingest API over HTTP
- wire GtfsResolver into publish pipeline
- add GtfsResolver to map train_num to trip_id
- implement main polling loop with MQTT reconnect (phase 4)
- implement MQTT publisher (phase 3)
- implement Amtrak API client and data models (phase 2)
- add dependencies and project skeleton (phase 1)
- copier copy

### Fix

- **buswhere**: map the Albany Greenport and Columbiaville timepoints
- **buswhere**: correct guessed slug for the Albany A AM candidate route
- **buswhere**: attribute each device to the route it is running
- possibly fix buswhere stop matching
- **buswhere**: stop treating buswhere's device name as a unique vehicle id
- fix buswhere up a bit
- **amtrak**: read Velocity for speed and parse 12-hour LastValTS
- tolerate non-numeric stop_eta values and isolate buswhere route failures
- anchor GTFS service day in agency timezone, publish delays
- fix date cmp
- username in topics

### Refactor

- rename the package to gtfs-zone-rt-pollers
- extract Source abstraction, thin publish layer
